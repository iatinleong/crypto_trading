import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from market import MarketClient
from paper_engine import PaperEngine
from state_store import load_state, save_state
from backtest_engine import full_analysis

market = MarketClient()
engine = PaperEngine(load_state())
price_cache: dict[str, float] = {}

# 已連線的前端 WS 客戶端：key = "SYMBOL/interval"
ws_clients: dict[str, set[WebSocket]] = {}

# 已啟動的自動策略：key = "SYMBOL/interval" -> {risk_pct, leverage, taker_fee, last_signal_key}
# 純記憶體狀態（不落地持久化），伺服器重啟就需要重新啟動策略
armed_strategies: dict[str, dict] = {}


async def funding_loop():
    """每分鐘檢查一次是否跨過 00:00/08:00/16:00 UTC 結算點，對持倉收付資金費。"""
    while True:
        try:
            buckets = engine.due_funding_buckets()
            if buckets:
                for symbol in list(engine.positions.keys()):
                    try:
                        info = await market.get_current_funding_rate(symbol)
                        for _ in buckets:
                            engine.apply_funding(symbol, info["funding_rate"], info["mark_price"])
                    except Exception as e:
                        print(f"[funding] {symbol}: {e}")
                save_state(engine.to_dict())
        except Exception as e:
            print(f"[funding] {e}")
        await asyncio.sleep(60)


async def strategy_loop():
    """
    每 15 秒對每個已啟動的自動策略重跑一次纏論訊號生成（跟回測同一套 full_analysis），
    若最新訊號的進場點就落在最新一根K棒（代表訊號剛確認），且還沒對這個訊號下過單，
    就用目前市價自動下單（帶入該訊號的 SL/TP），倉位大小依單筆風險 risk_pct 反推。
    """
    while True:
        for key, cfg in list(armed_strategies.items()):
            symbol, interval = key.split("/", 1)
            try:
                klines = await market.get_klines(symbol, interval, limit=500)
                if len(klines) < 50:
                    continue
                result = full_analysis(klines, interval=interval, taker_fee=cfg["taker_fee"])
                signals = result["signals"]
                if not signals:
                    continue

                latest = signals[-1]
                if latest["index"] < len(klines) - 2:
                    continue  # 訊號不是剛發生的，不追歷史訊號

                if symbol in engine.positions:
                    continue  # 跟回測一致：同一幣對已有持倉時不追加新訊號

                sig_key = f"{latest['time']}_{latest['type']}"
                if cfg.get("last_signal_key") == sig_key:
                    continue  # 這個訊號已經處理過
                cfg["last_signal_key"] = sig_key

                sl_dist = abs(latest["entry"] - latest["sl"])
                if sl_dist < latest["entry"] * 0.0001:
                    continue  # 止損距離太近，視為無效訊號（跟回測門檻一致）

                current_price = await market.get_price(symbol)
                price_cache[symbol] = current_price
                account = engine.get_account(price_cache)
                risk_amount = account["totalWalletBalance"] * cfg["risk_pct"]
                qty = risk_amount / sl_dist

                # 保證金上限：用 availableBalance（尚未被任何持倉鎖住的自由資金）而非
                # totalWalletBalance，因為實測允許同時對多個幣對啟動策略，若用總資產當
                # 基準，各策略會各自以為能用到 20% 總資產，加總起來可能遠超單一回測模型
                notional = qty * current_price
                margin   = notional / cfg["leverage"]
                max_margin = account["availableBalance"] * 0.20
                if margin > max_margin:
                    margin   = max_margin
                    notional = margin * cfg["leverage"]
                    qty      = notional / current_price

                engine.place_order(
                    symbol=symbol, side=latest["side"], order_type="MARKET",
                    quantity=qty, leverage=cfg["leverage"], current_price=current_price,
                    sl=latest["sl"], tp=latest["tp"],
                )
                save_state(engine.to_dict())
                print(f"[strategy] {key} 自動下單 {latest['type']} {latest['side']} qty={qty:.6f} @ {current_price}")
            except Exception as e:
                print(f"[strategy] {key}: {e}")
        await asyncio.sleep(15)


async def poll_loop():
    """每 500ms 輪詢 Binance REST → 推送給前端；同時檢查持倉的強平/止盈止損（繞過 WS 封鎖）"""
    while True:
        watched_symbols: set[str] = set()

        for key, clients in list(ws_clients.items()):
            if not clients:
                continue
            symbol, interval = key.split("/", 1)
            watched_symbols.add(symbol)
            try:
                klines = await market.get_klines(symbol, interval, limit=1)
                if not klines:
                    continue
                k = klines[0]
                price = k["close"]
                price_cache[symbol] = price

                liquidated = engine.check_liquidation(symbol, price)
                filled = engine.check_limit_orders(symbol, price)
                closed = False if liquidated else engine.check_sl_tp(symbol, price)
                if filled or closed or liquidated:
                    save_state(engine.to_dict())

                msg = json.dumps({"type": "tick", "price": price, "kline": k})
                dead: set[WebSocket] = set()
                for ws in list(clients):
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.add(ws)
                clients -= dead
            except Exception as e:
                print(f"[poll] {key}: {e}")

        # 有持倉但沒有開圖表監看的幣對，仍要輪詢價格以檢查強平/止盈止損
        for symbol in list(engine.positions.keys()):
            if symbol in watched_symbols:
                continue
            try:
                price = await market.get_price(symbol)
                price_cache[symbol] = price
                liquidated = engine.check_liquidation(symbol, price)
                filled = engine.check_limit_orders(symbol, price)
                closed = False if liquidated else engine.check_sl_tp(symbol, price)
                if filled or closed or liquidated:
                    save_state(engine.to_dict())
            except Exception as e:
                print(f"[poll-bg] {symbol}: {e}")

        await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    funding_task = asyncio.create_task(funding_loop())
    strategy_task = asyncio.create_task(strategy_loop())
    yield
    task.cancel()
    funding_task.cancel()
    strategy_task.cancel()


app = FastAPI(title="賽博纏論 Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderRequest(BaseModel):
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: Optional[float] = None
    leverage: int = 10
    sl: Optional[float] = None
    tp: Optional[float] = None


class SlTpRequest(BaseModel):
    sl: Optional[float] = None
    tp: Optional[float] = None


class StrategyRequest(BaseModel):
    symbol: str
    interval: str = "1h"
    risk_pct: float = 0.01
    leverage: int = 10
    taker_fee: float = 0.0005


# ── 行情 ───────────────────────────────────────────────────────────────────

@app.get("/api/klines")
async def get_klines(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 200):
    try:
        return await market.get_klines(symbol, interval, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ticker")
async def get_ticker(symbol: str = "BTCUSDT"):
    try:
        return await market.get_ticker(symbol)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Paper Trading ──────────────────────────────────────────────────────────

@app.get("/api/account")
async def get_account():
    return engine.get_account(price_cache)


@app.get("/api/positions")
async def get_positions():
    return engine.get_positions(price_cache)


@app.get("/api/orders")
async def get_orders(symbol: str = "BTCUSDT"):
    return engine.get_open_orders(symbol)


@app.get("/api/trades")
async def get_trades(symbol: str = "BTCUSDT", limit: int = 100):
    return engine.get_trade_history(symbol, limit)


@app.post("/api/order")
async def place_order(order: OrderRequest):
    try:
        current_price = price_cache.get(order.symbol)
        if current_price is None and order.order_type == "MARKET":
            current_price = await market.get_price(order.symbol)
            price_cache[order.symbol] = current_price

        result = engine.place_order(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            leverage=order.leverage,
            current_price=current_price,
            sl=order.sl,
            tp=order.tp,
        )
        save_state(engine.to_dict())
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/order/{order_id}")
async def cancel_order(order_id: int):
    try:
        result = engine.cancel_order(order_id)
        save_state(engine.to_dict())
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/position/{symbol}/sltp")
async def set_position_sl_tp(symbol: str, req: SlTpRequest):
    try:
        engine.set_sl_tp(symbol, req.sl, req.tp)
        save_state(engine.to_dict())
        return {"message": "已更新止盈止損"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── 自動策略（用回測同一套訊號生成，接上即時行情自動下單） ─────────────────

@app.post("/api/strategy/start")
async def start_strategy(req: StrategyRequest):
    key = f"{req.symbol}/{req.interval}"
    armed_strategies[key] = {
        "risk_pct": req.risk_pct,
        "leverage": req.leverage,
        "taker_fee": req.taker_fee,
        "last_signal_key": None,
    }
    return {"message": f"已啟動 {key} 自動策略", "armed": list(armed_strategies.keys())}


@app.post("/api/strategy/stop")
async def stop_strategy(symbol: str, interval: str):
    key = f"{symbol}/{interval}"
    armed_strategies.pop(key, None)
    return {"message": f"已停止 {key} 自動策略", "armed": list(armed_strategies.keys())}


@app.get("/api/strategy/status")
async def strategy_status():
    return {
        "armed": [
            {"key": k, "risk_pct": v["risk_pct"], "leverage": v["leverage"], "last_signal_key": v["last_signal_key"]}
            for k, v in armed_strategies.items()
        ]
    }


# ── Backtest ───────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    limit: int = 500
    initial_capital: float = 500.0
    leverage: int = 10
    risk_pct: float = 0.01
    taker_fee: float = 0.0005


@app.post("/api/backtest")
async def run_backtest_api(req: BacktestRequest):
    try:
        limit = min(req.limit, 50_000)
        klines = await market.get_klines_cached(req.symbol, req.interval, limit)
        try:
            funding_map = await market.get_funding_rates_cached(
                req.symbol, klines[0]["time"], klines[-1]["time"]
            )
        except Exception as e:
            print(f"[backtest] funding rate fetch failed, fallback to fixed rate: {e}")
            funding_map = None

        result = full_analysis(
            klines,
            initial_capital=req.initial_capital,
            leverage=req.leverage,
            risk_pct=req.risk_pct,
            interval=req.interval,
            taker_fee=req.taker_fee,
            funding_map=funding_map,
        )
        result["klines"] = klines
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reset")
async def reset_account():
    global engine
    engine = PaperEngine()
    save_state(engine.to_dict())
    return {"message": "帳戶已重置為 $500 USDT"}


# ── WebSocket：後端 poll → 前端推送 ────────────────────────────────────────

@app.websocket("/ws/{symbol}/{interval}")
async def ws_endpoint(websocket: WebSocket, symbol: str, interval: str):
    await websocket.accept()
    key = f"{symbol}/{interval}"
    ws_clients.setdefault(key, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()   # 保持連線（前端不需要送任何訊息）
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.get(key, set()).discard(websocket)


# ── 靜態前端 ───────────────────────────────────────────────────────────────
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
