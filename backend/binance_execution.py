"""
真實下單執行模組：把系統的交易決策（symbol/side/quantity/SL/TP）翻譯成 Binance
USDS-M 合約的真實 REST API 呼叫。

目前 main.py 的 strategy_loop() 完全沒有接這個模組，還是走 PaperEngine 的本地模擬
撮合——這是刻意的：先把這個模組獨立寫好、獨立測試過，再由使用者自己決定何時、
用什麼風險參數去接上正式系統，不會被動默默切換到真實下單。

已知的 Binance API 細節（詳見 chan_lun_audit_report.md 陷阱4.12/4.13）：
1. SL/TP（STOP_MARKET/TAKE_PROFIT_MARKET）已於 2025-12-09 遷移到獨立的 Algo Order API
   （POST /fapi/v1/algoOrder，algoType=CONDITIONAL，參數是 triggerPrice 不是 stopPrice），
   舊的 /fapi/v1/order 不再接受這幾種type。查詢/撤銷也要另外呼叫
   openAlgoOrders/algoOpenOrders，跟一般掛單是分開兩本簿子。
2. 本機時鐘只要跟伺服器差超過 recvWindow 就會被 -1021 拒絕（Windows 常見未校時），
   所以初始化時會主動對時、算 offset，每個 signed request 都用校正後的 timestamp。
3. 下單數量/價格要照每個symbol自己的 stepSize/tickSize/minNotional 取整，不能隨便
   round 到固定小數位——寫死3位小數對BTCUSDT剛好對，對別的幣種不一定對。

Testnet / Mainnet 切換：BinanceExecutionClient(testnet=True/False)。
金鑰讀 backend/.env 的 BINANCE_TESTNET_API_KEY/SECRET（testnet=True）
或 BINANCE_API_KEY/SECRET（testnet=False，真實資金，務必謹慎）。

直接執行本檔（`python binance_execution.py`）會對 Testnet 跑一輪自我測試
（跟 experiment/scripts/2026-07-02_testnet_order_api_check.py 相同流程，
但改成呼叫這個模組本身的方法，用來驗證模組正確性，不是重新寫一遍）。
"""
import hashlib
import hmac
import os
import sys
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TESTNET_BASE_URL = "https://testnet.binancefuture.com"
MAINNET_BASE_URL = "https://fapi.binance.com"
_CLIENT_KW = dict(verify=False)  # 同 market.py：Windows 有時缺中繼憑證


class BinanceAPIError(RuntimeError):
    """Binance 回傳非 2xx 時拋出，帶原始 code/msg 方便呼叫端判斷要不要重試。"""

    def __init__(self, status: int, code, msg: str):
        self.status = status
        self.code = code
        self.msg = msg
        super().__init__(f"[{status}] code={code} msg={msg}")


class BinanceExecutionClient:
    def __init__(self, testnet: bool = True):
        self.testnet = testnet
        self.base_url = TESTNET_BASE_URL if testnet else MAINNET_BASE_URL
        prefix = "BINANCE_TESTNET_" if testnet else "BINANCE_"
        self.api_key = os.environ.get(f"{prefix}API_KEY")
        self.api_secret = os.environ.get(f"{prefix}API_SECRET")
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                f"缺少 {prefix}API_KEY / {prefix}API_SECRET，請寫進 backend/.env"
            )
        self._client = httpx.Client(timeout=10, **_CLIENT_KW)
        self._time_offset_ms = 0
        self._symbol_filters: dict[str, dict] = {}
        self._sync_server_time()

    # ── 底層：簽名 / 對時 / 請求 ──────────────────────────────────────────

    def _sync_server_time(self):
        """本機時鐘漂移超過 recvWindow 就會被 -1021 拒絕，主動對時校正，
        不用去動作業系統時鐘設定（見陷阱4.13）。"""
        data = self._request("GET", "/fapi/v1/time", signed=False)
        server_ms = data["serverTime"]
        local_ms = int(time.time() * 1000)
        self._time_offset_ms = server_ms - local_ms

    def _sign(self, params: dict) -> dict:
        params = {
            **params,
            "timestamp": int(time.time() * 1000) + self._time_offset_ms,
            "recvWindow": 5000,
        }
        query = urllib.parse.urlencode(params)
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        headers = {"X-MBX-APIKEY": self.api_key}
        res = self._client.request(method, f"{self.base_url}{path}", params=params, headers=headers)
        try:
            data = res.json()
        except Exception:
            data = res.text
        if res.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else str(data)
            raise BinanceAPIError(res.status_code, code, msg)
        return data

    # ── 交易對精度規則（stepSize/tickSize/minNotional） ───────────────────

    def _get_symbol_filters(self, symbol: str) -> dict:
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]
        info = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info["symbols"]:
            if s["symbol"] != symbol:
                continue
            filters = {f["filterType"]: f for f in s["filters"]}
            result = {
                "stepSize": Decimal(filters["LOT_SIZE"]["stepSize"]),
                "minQty": Decimal(filters["LOT_SIZE"]["minQty"]),
                "tickSize": Decimal(filters["PRICE_FILTER"]["tickSize"]),
                "minNotional": Decimal(filters.get("MIN_NOTIONAL", {}).get("notional", "0")),
            }
            self._symbol_filters[symbol] = result
            return result
        raise ValueError(f"exchangeInfo 找不到 {symbol}")

    @staticmethod
    def _round_step(value: float, step: Decimal) -> float:
        """無條件捨去到 step 的倍數，避免因為進位超過交易所允許的最小增量而被拒單。"""
        d = Decimal(str(value))
        return float((d // step) * step)

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self._get_symbol_filters(symbol)
        rounded = self._round_step(qty, f["stepSize"])
        if rounded < float(f["minQty"]):
            raise ValueError(f"{symbol} 數量 {qty} 取整後 {rounded} 小於最小下單量 {f['minQty']}")
        return rounded

    def round_price(self, symbol: str, price: float) -> float:
        f = self._get_symbol_filters(symbol)
        return self._round_step(price, f["tickSize"])

    def check_min_notional(self, symbol: str, qty: float, price: float):
        f = self._get_symbol_filters(symbol)
        notional = Decimal(str(qty)) * Decimal(str(price))
        if notional < f["minNotional"]:
            raise ValueError(f"{symbol} 名目金額 {notional} 小於最低下單金額 {f['minNotional']}")

    # ── 帳戶／持倉查詢 ────────────────────────────────────────────────────

    def get_account(self) -> dict:
        return self._request("GET", "/fapi/v2/account")

    def get_mark_price(self, symbol: str) -> float:
        data = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        return float(data["price"])

    def get_position(self, symbol: str) -> dict | None:
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for p in rows:
            if float(p["positionAmt"]) != 0:
                return p
        return None

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    # ── 下單／SL-TP／平倉／撤單 ───────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        qty = self.round_qty(symbol, quantity)
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
        })

    def set_stop_loss_take_profit(self, symbol: str, position_side: str,
                                   sl: float | None, tp: float | None) -> tuple[dict | None, dict | None]:
        """position_side 是「持倉方向」（BUY=多單/SELL=空單），出場單方向自動取相反邊。
        走新版 Algo Order API（陷阱4.12），closePosition=true 平掉當時的全部持倉，不可帶quantity。"""
        exit_side = "SELL" if position_side == "BUY" else "BUY"
        sl_order = tp_order = None
        if sl is not None:
            sl_order = self._request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": exit_side, "type": "STOP_MARKET",
                "triggerPrice": self.round_price(symbol, sl), "closePosition": "true",
            })
        if tp is not None:
            tp_order = self._request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": exit_side, "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": self.round_price(symbol, tp), "closePosition": "true",
            })
        return sl_order, tp_order

    def close_position(self, symbol: str) -> dict | None:
        pos = self.get_position(symbol)
        if pos is None:
            return None
        amt = float(pos["positionAmt"])
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": "SELL" if amt > 0 else "BUY",
            "type": "MARKET", "quantity": abs(amt), "reduceOnly": "true",
        })

    def cancel_all_orders(self, symbol: str):
        """一般掛單跟algo掛單是分開兩本簿子，各自要撤（陷阱4.12）。"""
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def get_open_orders(self, symbol: str) -> list:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol})

    def get_open_algo_orders(self, symbol: str) -> list:
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})

    # ── 高階介面：對齊 PaperEngine.place_order() 的呼叫方式 ─────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        leverage: int = 10,
        sl: float | None = None,
        tp: float | None = None,
        source: str = "MANUAL",
    ) -> dict:
        """介面盡量貼近 PaperEngine.place_order()，方便未來替換；
        目前只支援 order_type="MARKET"（跟 strategy_loop() 現行用法一致）。
        注意：這裡不處理「反手」的合併下單邏輯（PaperEngine._fill 的反向開倉分支），
        那需要先查現有持倉、算淨變動量，屬於呼叫端（strategy_loop）的責任，不在這個
        執行模組裡隱含發生，避免在下真實單的模組裡藏太多隱性行為。
        """
        if order_type != "MARKET":
            raise NotImplementedError("目前只支援 MARKET 單，跟 strategy_loop() 現行行為一致")

        self.set_leverage(symbol, leverage)
        order = self.place_market_order(symbol, side, quantity)
        if sl is not None or tp is not None:
            self.set_stop_loss_take_profit(symbol, side, sl, tp)
        order["source"] = source
        return order


# ════════════════════════════════════════════════════════════════════════════
# 自我測試：對 Testnet 跑一輪完整流程，驗證模組本身正確
# ════════════════════════════════════════════════════════════════════════════

def _self_test():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=== binance_execution.py 自我測試（Testnet）===")

    client = BinanceExecutionClient(testnet=True)
    symbol = "BTCUSDT"

    # 清掉可能殘留的舊倉位/掛單，確保乾淨起點
    client.cancel_all_orders(symbol)
    stale = client.close_position(symbol)
    if stale:
        print(f"[清理] 平掉殘留倉位: {stale.get('avgPrice')}")

    account = client.get_account()
    print(f"[帳戶] availableBalance={account['availableBalance']}")

    price = client.get_mark_price(symbol)
    qty = client.round_qty(symbol, max(150.0 / price, 0.002))
    client.check_min_notional(symbol, qty, price)
    print(f"[開倉] {symbol} 市價≈{price}，測試數量={qty}")

    order = client.place_order(
        symbol, "BUY", "MARKET", qty, leverage=5,
        sl=round(price * 0.97, 1), tp=round(price * 1.03, 1), source="SELF_TEST",
    )
    print(f"[開倉結果] orderId={order.get('orderId')} status={order.get('status')}")

    pos = client.get_position(symbol)
    print(f"[持倉查詢] {pos}")

    open_orders = client.get_open_orders(symbol)
    algo_orders = client.get_open_algo_orders(symbol)
    print(f"[掛單查詢] 一般單={len(open_orders)} algo單={len(algo_orders)}")
    assert len(algo_orders) == 2, f"預期SL+TP兩張algo單，實際{len(algo_orders)}張"

    closed = client.close_position(symbol)
    print(f"[平倉] {closed.get('status') if closed else '無持倉'}")

    client.cancel_all_orders(symbol)
    final_pos = client.get_position(symbol)
    final_orders = client.get_open_orders(symbol) + client.get_open_algo_orders(symbol)
    print(f"[收尾] 持倉={final_pos} 剩餘掛單數={len(final_orders)}")
    assert final_pos is None, "收尾後應該沒有持倉"
    assert len(final_orders) == 0, "收尾後應該沒有剩餘掛單"

    print("\n=== 自我測試全部通過 ===")


if __name__ == "__main__":
    _self_test()
