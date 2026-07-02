"""
賽博纏論 回測引擎
通用回測模擬器（進場管理／SL-TP／移動止損止盈／反手／滑價／資金費／保證金）＋
策略分派入口 full_analysis()。策略本身（缠論的分型/笔/中枢/背驰/訊號產生）
已經抽到 backend/strategies/ 底下，這裡完全不知道訊號是怎麼被算出來的，只吃
一份通用格式的 signals 清單——新增其他策略時，這個檔案不用改，只要在
strategies/__init__.py 的 STRATEGIES 註冊新策略的 analyze() 即可。

費用仿照 Binance USDT 永續合約真實狀況
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from strategies import STRATEGIES

# ── Binance 永續合約費率 ────────────────────────────────────────────────────
TAKER_FEE   = 0.0005   # 0.05% — 市價 / 止損觸發
MAKER_FEE   = 0.0002   # 0.02% — 限價
FUNDING_RATE = 0.0001  # 0.01% per 8h（BTC 典型值，可正可負）

INTERVAL_HOURS: Dict[str, float] = {
    "1m": 1/60, "3m": 3/60, "5m": 5/60, "15m": 15/60, "30m": 0.5,
    "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "1d": 24,
}


# ════════════════════════════════════════════════════════════════════════════
# 回測引擎 (Backtest Engine)
# ════════════════════════════════════════════════════════════════════════════

def run_backtest(
    klines:          List[Dict],
    signals:         List[Dict],
    initial_capital: float = 500.0,
    leverage:        int   = 10,
    risk_pct:        float = 0.01,
    interval:        str   = "1h",
    taker_fee:       float = TAKER_FEE,          # 可自訂（VIP等級）
    funding_map:     Optional[Dict[int, float]] = None,  # {unix_sec: rate} 真實費率
    margin_cap_pct:      float = 0.20,   # 單筆保證金上限（佔當時資金比例）
    reversal_on_opposite: bool = False,  # True：持倉中出現反方向訊號時，立即平倉並反手開反方向倉
    trailing_stop:        bool = False,  # True：啟用移動止損（達到獲利門檻後，止損隨最優價位上移/下移）
    trailing_activate_r:  float = 1.0,   # 移動止損啟動門檻（以原始 R＝進場與止損距離為單位）
    trailing_distance_r:  float = 1.0,   # 啟動後，止損與目前最優價位維持的距離（同樣以 R 為單位）
    trailing_disable_fixed_tp: bool = False,  # True：啟用移動止損後不再檢查原始固定TP，只靠移動止損出場（純粹讓利潤奔跑）
    trailing_tp:          bool = False,  # True：啟用移動止盈（達到獲利門檻後，止盈目標隨最優價位往更遠處延伸）
    trailing_tp_activate_r: float = 1.0,  # 移動止盈啟動門檻（以R為單位）
    trailing_tp_distance_r: float = 1.0,  # 啟動後，止盈目標與目前最優價位維持的距離（以R為單位，目標會持續被往外推）
    slippage_pct:          float = 0.0,   # 進出場不利滑價（例如0.0003＝0.03%），開倉/平倉一律往對自己不利的方向偏移
) -> Dict[str, Any]:
    """
    funding_map: 由 Binance /fapi/v1/fundingRate 取得的歷史費率 dict。
                 若為 None 則沿用固定 FUNDING_RATE 常數（0.01%/8h）。

    資金費每 8h 在 00:00 / 08:00 / 16:00 UTC 結算，且只在結算當下持有倉位才會
    被收取或支付（正費率：多付空收；負費率：空付多收）。故以 UTC 絕對時間切
    出的「結算 bucket」判斷是否跨過結算點，而非以進場後經過幾根K棒判斷。
    """
    FUNDING_INTERVAL_SEC = 8 * 3600

    # 將 funding_map 的 key 排序，用於二分搜尋
    fund_times = sorted(funding_map.keys()) if funding_map else []

    def _funding_rate_at(settle_time: int) -> float:
        """取得 <= settle_time 的最近一筆真實資金費率（無資料則用固定值）。"""
        if not funding_map or not fund_times:
            return FUNDING_RATE
        lo, hi = 0, len(fund_times) - 1
        idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if fund_times[mid] <= settle_time:
                idx = mid; lo = mid + 1
            else:
                hi = mid - 1
        return funding_map[fund_times[idx]] if idx != -1 else FUNDING_RATE

    def _get_funding(settle_time: int, notional: float, side: str) -> float:
        """依方向計算單次結算的資金費用（正值＝成本，負值＝收入）。"""
        rate = _funding_rate_at(settle_time)
        return notional * rate if side == "BUY" else -notional * rate

    def _slip(price: float, side: str, closing: bool) -> float:
        """
        往對自己不利的方向偏移 slippage_pct：開多／平空 = 買方，價格往上偏才不利；
        開空／平多 = 賣方，價格往下偏才不利。closing=True 代表這次成交是為了平倉
        （方向與 side 相反），closing=False 代表是照 side 方向開倉。
        """
        if slippage_pct <= 0:
            return price
        worse_is_lower = (side == "BUY") == closing
        return price * (1 - slippage_pct) if worse_is_lower else price * (1 + slippage_pct)

    capital      = initial_capital
    trades:       List[Dict] = []
    equity_curve: List[Dict] = [{"time": klines[0]["time"], "equity": capital}]

    sig_map = {s["index"]: s for s in signals}
    active:  Optional[Dict] = None
    n = len(klines)

    for i, k in enumerate(klines):

        # ── 管理現有倉位 ──────────────────────────────────────────────────
        if active is not None:

            # 資金費率結算：只在真正跨過 00:00/08:00/16:00 UTC 結算點時收付
            cur_bucket = k["time"] // FUNDING_INTERVAL_SEC
            if cur_bucket > active["last_funding_bucket"]:
                notional_now = active["qty"] * k["close"]
                for b in range(active["last_funding_bucket"] + 1, cur_bucket + 1):
                    settle_time = b * FUNDING_INTERVAL_SEC
                    active["funding_fees"] += _get_funding(settle_time, notional_now, active["side"])
                active["last_funding_bucket"] = cur_bucket

            tp_before_update = active["tp"]
            # 移動止損／移動止盈共用「目前最優價位」的追蹤（只要任一功能開啟就要更新）
            if trailing_stop or trailing_tp:
                sl_dist0 = active["sl_dist"]
                if active["side"] == "BUY":
                    active["best_price"] = max(active.get("best_price", active["entry"]), k["high"])
                    profit_r = (active["best_price"] - active["entry"]) / sl_dist0
                else:
                    active["best_price"] = min(active.get("best_price", active["entry"]), k["low"])
                    profit_r = (active["entry"] - active["best_price"]) / sl_dist0

                # 移動止損：達到 trailing_activate_r 個 R 的獲利後，止損跟著最優價位移動，
                # 與最優價位維持 trailing_distance_r 個 R 的距離（只會往有利方向移動，不會鬆開）
                if trailing_stop and profit_r >= trailing_activate_r:
                    if active["side"] == "BUY":
                        candidate_sl = active["best_price"] - trailing_distance_r * sl_dist0
                        if candidate_sl > active["sl"]:
                            active["sl"] = candidate_sl
                    else:
                        candidate_sl = active["best_price"] + trailing_distance_r * sl_dist0
                        if candidate_sl < active["sl"]:
                            active["sl"] = candidate_sl

                # 移動止盈：達到 trailing_tp_activate_r 個 R 的獲利後，止盈目標跟著最優價位
                # 往更遠處延伸，維持 trailing_tp_distance_r 個 R 的距離（目標只會往外推，不會縮回）
                if trailing_tp and profit_r >= trailing_tp_activate_r:
                    if active["side"] == "BUY":
                        candidate_tp = active["best_price"] + trailing_tp_distance_r * sl_dist0
                        if candidate_tp > active["tp"]:
                            active["tp"] = candidate_tp
                    else:
                        candidate_tp = active["best_price"] - trailing_tp_distance_r * sl_dist0
                        if candidate_tp < active["tp"]:
                            active["tp"] = candidate_tp

            # 止盈 / 止損判斷（trailing_disable_fixed_tp 開啟且未使用移動止盈時，完全不檢查
            # 固定TP，只靠移動止損出場，讓利潤有機會超越原始結構性TP目標）
            hit_sl = hit_tp = False
            exit_price = 0.0
            check_tp = trailing_tp or not (trailing_stop and trailing_disable_fixed_tp)

            if active["side"] == "BUY":
                if k["low"] <= active["sl"]:
                    hit_sl = True; exit_price = _slip(active["sl"], active["side"], closing=True)
                elif check_tp and k["high"] >= tp_before_update:
                    hit_tp = True; exit_price = _slip(tp_before_update, active["side"], closing=True)
            else:
                if k["high"] >= active["sl"]:
                    hit_sl = True; exit_price = _slip(active["sl"], active["side"], closing=True)
                elif check_tp and k["low"] <= tp_before_update:
                    hit_tp = True; exit_price = _slip(tp_before_update, active["side"], closing=True)

            if hit_sl or hit_tp:
                exit_fee = active["qty"] * exit_price * taker_fee
                if active["side"] == "BUY":
                    raw_pnl = active["qty"] * (exit_price - active["entry"])
                else:
                    raw_pnl = active["qty"] * (active["entry"] - exit_price)

                total_fees = active["entry_fee"] + exit_fee + active["funding_fees"]
                net_pnl    = raw_pnl - total_fees
                capital   += active["margin"] + net_pnl

                trade = {**active,
                    "exit_price":  exit_price,
                    "exit_time":   k["time"],
                    "exit_index":  i,
                    "exit_reason": "TP" if hit_tp else "SL",
                    "raw_pnl":     raw_pnl,
                    "total_fees":  total_fees,
                    "pnl":         net_pnl,
                    "pnl_pct":     net_pnl / active["margin"] * 100 if active["margin"] > 0 else 0,
                    "exit_fee":    exit_fee,
                }
                trades.append(trade)
                active = None
                equity_curve.append({"time": k["time"], "equity": capital})

            # 反手邏輯：持倉中若出現反方向新訊號，立即以該訊號的進場價平掉現有倉位，
            # 讓下方「開新倉」區塊用同一個訊號反手開反方向倉（同方向訊號仍照舊忽略，不加倉）
            if active is not None and reversal_on_opposite and i in sig_map and sig_map[i]["side"] != active["side"]:
                exit_price = _slip(sig_map[i]["entry"], active["side"], closing=True)
                exit_fee = active["qty"] * exit_price * taker_fee
                if active["side"] == "BUY":
                    raw_pnl = active["qty"] * (exit_price - active["entry"])
                else:
                    raw_pnl = active["qty"] * (active["entry"] - exit_price)
                total_fees = active["entry_fee"] + exit_fee + active["funding_fees"]
                net_pnl = raw_pnl - total_fees
                capital += active["margin"] + net_pnl
                trades.append({**active,
                    "exit_price":  exit_price,
                    "exit_time":   k["time"],
                    "exit_index":  i,
                    "exit_reason": "REVERSAL",
                    "raw_pnl":     raw_pnl,
                    "total_fees":  total_fees,
                    "pnl":         net_pnl,
                    "pnl_pct":     net_pnl / active["margin"] * 100 if active["margin"] > 0 else 0,
                    "exit_fee":    exit_fee,
                })
                equity_curve.append({"time": k["time"], "equity": capital})
                active = None

        # ── 開新倉 ──────────────────────────────────────────────────────────
        if active is None and i in sig_map:
            sig = sig_map[i]
            entry  = _slip(sig["entry"], sig["side"], closing=False)
            sl_prc = sig["sl"]
            sl_dist = abs(entry - sl_prc)
            if sl_dist < entry * 0.0001:
                continue

            risk_amount = capital * risk_pct
            qty         = risk_amount / sl_dist
            notional    = qty * entry
            margin      = notional / leverage

            if margin > capital * margin_cap_pct:
                margin   = capital * margin_cap_pct
                notional = margin * leverage
                qty      = notional / entry

            if capital < margin:
                continue

            entry_fee = notional * taker_fee
            capital  -= (margin + entry_fee)

            active = {
                "signal_type": sig["type"],
                "side":        sig["side"],
                "entry":       entry,
                "sl":          sl_prc,
                "tp":          sig["tp"],
                "sl_dist":     sl_dist,
                "qty":         qty,
                "notional":    notional,
                "margin":      margin,
                "leverage":    leverage,
                "entry_fee":   entry_fee,
                "funding_fees": 0.0,
                "last_funding_bucket": k["time"] // FUNDING_INTERVAL_SEC,
                "entry_time":  k["time"],
                "entry_index": i,
                "reason":      sig["reason"],
            }

    # 回測結束：強制平倉
    if active is not None:
        last = klines[-1]
        ep   = last["close"]
        ef   = active["qty"] * ep * taker_fee
        rpnl = active["qty"] * (ep - active["entry"]) if active["side"] == "BUY" \
               else active["qty"] * (active["entry"] - ep)
        fees = active["entry_fee"] + ef + active["funding_fees"]
        npnl = rpnl - fees
        capital += active["margin"] + npnl
        trades.append({**active,
            "exit_price":  ep, "exit_time": last["time"],
            "exit_index":  n - 1, "exit_reason": "CLOSE",
            "raw_pnl":     rpnl, "total_fees": fees,
            "pnl":         npnl,
            "pnl_pct":     npnl / active["margin"] * 100 if active["margin"] > 0 else 0,
            "exit_fee":    ef,
        })
        equity_curve.append({"time": last["time"], "equity": capital})

    # ── 統計 ────────────────────────────────────────────────────────────────
    winners = [t for t in trades if t["pnl"] > 0]
    losers  = [t for t in trades if t["pnl"] <= 0]
    tp_wins = [t for t in trades if t.get("exit_reason") == "TP"]
    sl_loss = [t for t in trades if t.get("exit_reason") == "SL"]
    tot_profit = sum(t["pnl"] for t in winners)
    tot_loss   = abs(sum(t["pnl"] for t in losers))
    tot_fees   = sum(t["total_fees"] for t in trades)
    tot_trading_fee = sum(t["entry_fee"] + t["exit_fee"] for t in trades)
    tot_funding_fee = sum(t["funding_fees"] for t in trades)

    peak  = initial_capital
    max_dd = 0.0
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        dd   = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # 平均盈亏比
    avg_win  = tot_profit / len(winners) if winners else 0
    avg_loss = tot_loss   / len(losers)  if losers  else 0
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    stats = {
        "initial_capital": initial_capital,
        "final_capital":   capital,
        "total_pnl":       capital - initial_capital,
        "total_return":    (capital - initial_capital) / initial_capital * 100,
        "total_trades":    len(trades),
        "win_count":       len(winners),
        "loss_count":      len(losers),
        "tp_count":        len(tp_wins),
        "sl_count":        len(sl_loss),
        "win_rate":        len(winners) / len(trades) * 100 if trades else 0,
        "profit_factor":   tot_profit / tot_loss if tot_loss > 0 else 999.0,
        "avg_win":         avg_win,
        "avg_loss":        avg_loss,
        "rr_ratio":        rr_ratio,
        "max_drawdown":    max_dd,
        "total_fees":      tot_fees,
        "total_trading_fee": tot_trading_fee,
        "total_funding_fee": tot_funding_fee,
    }

    return {"trades": trades, "equity_curve": equity_curve, "stats": stats}


# ════════════════════════════════════════════════════════════════════════════
# 主入口：依策略名稱分派 + 跑通用回測
# ════════════════════════════════════════════════════════════════════════════

def full_analysis(
    klines:               List[Dict],
    initial_capital:      float = 500.0,
    leverage:             int   = 10,
    risk_pct:             float = 0.01,
    interval:             str   = "1h",
    taker_fee:            float = TAKER_FEE,
    funding_map:          Optional[Dict[int, float]] = None,
    filter_counter_trend: bool = False,
    reversal_on_opposite: bool = True,   # 持倉中出現反方向訊號時，立即平倉反手（實測用真實資料驗證過是正面改動）
    slippage_pct:         float = 0.001, # 進出場不利滑價，預設0.1%（BTCUSDT正常~壓力情境交界，偏保守假設，見陷阱4.x）
    strategy:              str  = "chanlun",  # 策略名稱，對應 strategies.STRATEGIES 的 key
) -> Dict[str, Any]:
    analyze_fn = STRATEGIES.get(strategy)
    if analyze_fn is None:
        raise ValueError(f"未知策略: {strategy}（可用: {list(STRATEGIES.keys())}）")

    # 前端K棒圖 / MACD副圖一律顯示原始K棒，跟回測執行（進場、SL/TP、資金費）用同一份資料
    analysis = analyze_fn(klines, filter_counter_trend=filter_counter_trend)
    signals  = analysis["signals"]

    bt = run_backtest(
        klines, signals, initial_capital, leverage, risk_pct, interval,
        taker_fee=taker_fee, funding_map=funding_map,
        reversal_on_opposite=reversal_on_opposite,
        slippage_pct=slippage_pct,
    )

    return {**analysis, **bt}
