"""
賽博纏論 回測引擎
實作缠论核心：分型 → 笔 → 中枢 → 背驰 → 买卖点
費用仿照 Binance USDT 永續合約真實狀況
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple

# ── Binance 永續合約費率 ────────────────────────────────────────────────────
TAKER_FEE   = 0.0005   # 0.05% — 市價 / 止損觸發
MAKER_FEE   = 0.0002   # 0.02% — 限價
FUNDING_RATE = 0.0001  # 0.01% per 8h（BTC 典型值，可正可負）

INTERVAL_HOURS: Dict[str, float] = {
    "1m": 1/60, "3m": 3/60, "5m": 5/60, "15m": 15/60, "30m": 0.5,
    "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "1d": 24,
}


# ════════════════════════════════════════════════════════════════════════════
# MACD
# ════════════════════════════════════════════════════════════════════════════

def _ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)   # type: ignore[operator]
    return out


def compute_macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    sig: int = 9,
) -> Tuple[List, List, List]:
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    macd = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_f, ema_s)
    ]
    first_valid = next((i for i, v in enumerate(macd) if v is not None), len(macd))
    raw_sig = _ema([v for v in macd[first_valid:] if v is not None], sig)
    signal: List[Optional[float]] = [None] * first_valid + raw_sig
    hist = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd, signal)
    ]
    return macd, signal, hist


# ════════════════════════════════════════════════════════════════════════════
# 顶底分型 (Fractal)
# ════════════════════════════════════════════════════════════════════════════

def detect_fractals(klines: List[Dict]) -> List[Dict]:
    """
    顶分型: K[i-1].high < K[i].high > K[i+1].high
    底分型: K[i-1].low  > K[i].low  < K[i+1].low
    """
    result = []
    for i in range(1, len(klines) - 1):
        p, c, n = klines[i-1], klines[i], klines[i+1]
        if p["high"] < c["high"] and n["high"] < c["high"]:
            result.append({"index": i, "type": "top",    "price": c["high"], "time": c["time"]})
        elif p["low"] > c["low"] and n["low"] > c["low"]:
            result.append({"index": i, "type": "bottom", "price": c["low"],  "time": c["time"]})
    return result


def _merge_fractals(fractals: List[Dict]) -> List[Dict]:
    """強制交替方向，同方向取極值。"""
    if not fractals:
        return []
    merged = [fractals[0]]
    for f in fractals[1:]:
        last = merged[-1]
        if f["type"] == last["type"]:
            if (f["type"] == "top"    and f["price"] > last["price"]) or \
               (f["type"] == "bottom" and f["price"] < last["price"]):
                merged[-1] = f
        else:
            merged.append(f)
    return merged


# ════════════════════════════════════════════════════════════════════════════
# 笔 (Bi)
# ════════════════════════════════════════════════════════════════════════════

def detect_bi(klines: List[Dict], fractals: List[Dict]) -> List[Dict]:
    """
    有效笔：相鄰頂底分型之間至少相距 4 根 K 棒（含頭尾共 5 根以上）。
    """
    merged = _merge_fractals(fractals)
    bis = []
    for i in range(len(merged) - 1):
        f1, f2 = merged[i], merged[i+1]
        if f2["index"] - f1["index"] < 4:
            continue
        direction = "up" if f2["type"] == "top" else "down"
        bis.append({
            "start": f1, "end": f2,
            "direction": direction,
            "start_price": f1["price"],
            "end_price":   f2["price"],
        })
    return bis


# ════════════════════════════════════════════════════════════════════════════
# 中枢 (Zhongshu / Pivot Zone)
# ════════════════════════════════════════════════════════════════════════════

def detect_zhongshu(bis: List[Dict]) -> List[Dict]:
    """
    三筆重疊形成中枢：ZL = max(各笔低點), ZH = min(各笔高點), 且 ZH > ZL。
    中枢可被後續重疊的笔延伸。
    """
    def bi_range(b: Dict) -> Tuple[float, float]:
        lo = min(b["start_price"], b["end_price"])
        hi = max(b["start_price"], b["end_price"])
        return lo, hi

    raw: List[Dict] = []
    n = len(bis)
    for i in range(n - 2):
        b1, b2, b3 = bis[i], bis[i+1], bis[i+2]
        r1, r2, r3 = bi_range(b1), bi_range(b2), bi_range(b3)
        zl = max(r1[0], r2[0], r3[0])
        zh = min(r1[1], r2[1], r3[1])
        if zh <= zl:
            continue
        zs: Dict[str, Any] = {
            "zl": zl, "zh": zh,
            "start_time":  b1["start"]["time"],
            "end_time":    b3["end"]["time"],
            "start_index": b1["start"]["index"],
            "end_index":   b3["end"]["index"],
            "entry_dir":   b1["direction"],
        }
        # 延伸中枢：後續筆仍與核心重疊則併入
        j = i + 3
        while j < n:
            lo_j, hi_j = bi_range(bis[j])
            if lo_j <= zh and hi_j >= zl:
                zs["end_time"]  = bis[j]["end"]["time"]
                zs["end_index"] = bis[j]["end"]["index"]
                j += 1
            else:
                break
        raw.append(zs)

    # 去重：後中枢 start 在前中枢結束之前則合併
    deduped: List[Dict] = []
    for zs in raw:
        if deduped and zs["start_index"] <= deduped[-1]["end_index"]:
            prev = deduped[-1]
            if zs["end_index"] > prev["end_index"]:
                prev["end_time"]  = zs["end_time"]
                prev["end_index"] = zs["end_index"]
            prev["zl"] = min(prev["zl"], zs["zl"])
            prev["zh"] = max(prev["zh"], zs["zh"])
        else:
            deduped.append(zs)
    return deduped


# ════════════════════════════════════════════════════════════════════════════
# 背驰 (Beichi / MACD Divergence)
# ════════════════════════════════════════════════════════════════════════════

def _macd_area(bi: Dict, hist: List[Optional[float]]) -> float:
    """計算某筆內 MACD 柱狀圖的絕對面積（用於背驰判斷）。"""
    total = 0.0
    for i in range(bi["start"]["index"], bi["end"]["index"] + 1):
        h = hist[i]
        if h is not None:
            total += abs(h)
    return total


def _prev_same_dir(bis: List[Dict], idx: int) -> Optional[Dict]:
    direction = bis[idx]["direction"]
    for j in range(idx - 1, -1, -1):
        if bis[j]["direction"] == direction:
            return bis[j]
    return None


# ════════════════════════════════════════════════════════════════════════════
# 信号生成 (Signal Generation)
# ════════════════════════════════════════════════════════════════════════════

def generate_signals(
    klines:  List[Dict],
    bis:     List[Dict],
    zhongshu_list: List[Dict],
    hist:    List[Optional[float]],
) -> List[Dict]:
    signals: List[Dict] = []
    n = len(klines)

    def entry_at(idx: int) -> Tuple[float, int, int]:
        """下根 K 棒開盤作為入場。"""
        ni = min(idx + 1, n - 1)
        return klines[ni]["open"], ni, klines[ni]["time"]

    # ── B1 / S1：背驰买卖点 ────────────────────────────────────────────────
    for i, bi in enumerate(bis):
        end_idx = bi["end"]["index"]
        if end_idx >= n - 2:
            continue
        prev = _prev_same_dir(bis, i)
        if prev is None:
            continue

        curr_area = _macd_area(bi,   hist)
        prev_area = _macd_area(prev, hist)
        if prev_area < 1e-9:
            continue

        divergence = curr_area < prev_area * 0.85  # 至少弱 15%

        entry, ei, et = entry_at(end_idx)

        if bi["direction"] == "down" and bi["end"]["type"] == "bottom" and divergence:
            # 底背驰 → 第一类买点
            frac_low = bi["end_price"]
            sl = round(frac_low * 0.9970, 2)               # 分型低點下方 0.3%
            # TP：本波段下跌起點（上方最近頂分型）
            tp_raw = bi["start_price"]
            # 若有中枢阻力則取最近的
            zs_res = [z["zh"] for z in zhongshu_list
                      if z["zh"] > entry and z["start_index"] < end_idx]
            tp = min(zs_res) if zs_res else tp_raw
            if tp <= entry:                                 # 保底 2:1 R:R
                tp = round(entry + (entry - sl) * 2, 4)
            signals.append({
                "index": ei, "time": et,
                "type": "B1", "side": "BUY",
                "entry": entry, "sl": sl, "tp": tp,
                "reason": "第一类买点：底背驰",
                "fractal_price": frac_low, "fractal_time": bi["end"]["time"],
            })

        elif bi["direction"] == "up" and bi["end"]["type"] == "top" and divergence:
            # 顶背驰 → 第一类卖点
            frac_high = bi["end_price"]
            sl = round(frac_high * 1.0030, 2)              # 分型高點上方 0.3%
            tp_raw = bi["start_price"]
            zs_sup = [z["zl"] for z in zhongshu_list
                      if z["zl"] < entry and z["start_index"] < end_idx]
            tp = max(zs_sup) if zs_sup else tp_raw
            if tp >= entry:
                tp = round(entry - (sl - entry) * 2, 4)
            signals.append({
                "index": ei, "time": et,
                "type": "S1", "side": "SELL",
                "entry": entry, "sl": sl, "tp": tp,
                "reason": "第一类卖点：顶背驰",
                "fractal_price": frac_high, "fractal_time": bi["end"]["time"],
            })

    # ── B3 / S3：中枢突破回踩 ─────────────────────────────────────────────
    used: set = set()
    for zs in zhongshu_list:
        zs_end = zs["end_index"]
        if zs["zh"] <= zs["zl"]:
            continue
        search_end = min(zs_end + 60, n - 2)
        broke_up = broke_dn = False

        for j in range(zs_end, search_end):
            k = klines[j]

            if not broke_up and k["close"] > zs["zh"]:
                broke_up = True
                for k2i in range(j + 1, min(j + 40, n - 1)):
                    k2 = klines[k2i]
                    # 回踩至 ZH 附近（wick 觸碰但收盤在 ZH 以上）
                    if k2["low"] < zs["zh"] * 1.003 and k2["close"] > zs["zh"] * 0.997:
                        uid = (zs["start_index"], "B3")
                        if uid in used:
                            break
                        used.add(uid)
                        entry, ei, et = entry_at(k2i)
                        sl  = round(zs["zh"] * 0.9970, 4)
                        risk = max(entry - sl, entry * 0.001)
                        tp   = round(entry + risk * 2.5, 4)
                        signals.append({
                            "index": ei, "time": et,
                            "type": "B3", "side": "BUY",
                            "entry": entry, "sl": sl, "tp": tp,
                            "reason": "第三类买点：中枢上破回踩 ZH",
                            "zs_zl": zs["zl"], "zs_zh": zs["zh"],
                        })
                        break
                break

            if not broke_dn and k["close"] < zs["zl"]:
                broke_dn = True
                for k2i in range(j + 1, min(j + 40, n - 1)):
                    k2 = klines[k2i]
                    if k2["high"] > zs["zl"] * 0.997 and k2["close"] < zs["zl"] * 1.003:
                        uid = (zs["start_index"], "S3")
                        if uid in used:
                            break
                        used.add(uid)
                        entry, ei, et = entry_at(k2i)
                        sl   = round(zs["zl"] * 1.003, 4)
                        risk = max(sl - entry, entry * 0.001)
                        tp   = round(entry - risk * 2.5, 4)
                        signals.append({
                            "index": ei, "time": et,
                            "type": "S3", "side": "SELL",
                            "entry": entry, "sl": sl, "tp": tp,
                            "reason": "第三类卖点：中枢下破反抽 ZL",
                            "zs_zl": zs["zl"], "zs_zh": zs["zh"],
                        })
                        break
                break

    signals.sort(key=lambda x: x["index"])
    # 同一 K 棒只保留第一個信號
    seen: set = set()
    unique: List[Dict] = []
    for s in signals:
        if s["index"] not in seen:
            unique.append(s)
            seen.add(s["index"])
    return unique


# ════════════════════════════════════════════════════════════════════════════
# 回測引擎 (Backtest Engine)
# ════════════════════════════════════════════════════════════════════════════

def run_backtest(
    klines:          List[Dict],
    signals:         List[Dict],
    initial_capital: float = 10_000.0,
    leverage:        int   = 10,
    risk_pct:        float = 0.01,
    interval:        str   = "1h",
    taker_fee:       float = TAKER_FEE,          # 可自訂（VIP等級）
    funding_map:     Optional[Dict[int, float]] = None,  # {unix_sec: rate} 真實費率
) -> Dict[str, Any]:
    """
    funding_map: 由 Binance /fapi/v1/fundingRate 取得的歷史費率 dict。
                 若為 None 則沿用固定 FUNDING_RATE 常數（0.01%/8h）。
    """
    hours_per_candle    = INTERVAL_HOURS.get(interval, 1.0)
    candles_per_funding = max(1, round(8.0 / hours_per_candle))

    # 將 funding_map 的 key 排序，用於二分搜尋
    fund_times = sorted(funding_map.keys()) if funding_map else []

    def _get_funding(candle_time: int, notional: float) -> float:
        """取得最近一筆資金費率並計算費用。"""
        if not funding_map or not fund_times:
            return notional * FUNDING_RATE
        # 找 <= candle_time 的最新結算
        lo, hi = 0, len(fund_times) - 1
        idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if fund_times[mid] <= candle_time:
                idx = mid; lo = mid + 1
            else:
                hi = mid - 1
        if idx == -1:
            return notional * FUNDING_RATE
        rate = funding_map[fund_times[idx]]
        return notional * abs(rate)   # 空單費率可為負但仍為成本（取絕對值保守估計）

    capital      = initial_capital
    trades:       List[Dict] = []
    equity_curve: List[Dict] = [{"time": klines[0]["time"], "equity": capital}]

    sig_map = {s["index"]: s for s in signals}
    active:  Optional[Dict] = None
    n = len(klines)

    for i, k in enumerate(klines):

        # ── 管理現有倉位 ──────────────────────────────────────────────────
        if active is not None:
            held = i - active["entry_index"]

            # 資金費率結算（每 candles_per_funding 根一次）
            if held > 0 and held % candles_per_funding == 0:
                notional_now = active["qty"] * k["close"]
                funding = _get_funding(k["time"], notional_now)
                active["funding_fees"] += funding

            # 止盈 / 止損判斷
            hit_sl = hit_tp = False
            exit_price = 0.0

            if active["side"] == "BUY":
                if k["low"] <= active["sl"]:
                    hit_sl = True; exit_price = active["sl"]
                elif k["high"] >= active["tp"]:
                    hit_tp = True; exit_price = active["tp"]
            else:
                if k["high"] >= active["sl"]:
                    hit_sl = True; exit_price = active["sl"]
                elif k["low"] <= active["tp"]:
                    hit_tp = True; exit_price = active["tp"]

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

        # ── 開新倉 ──────────────────────────────────────────────────────────
        if active is None and i in sig_map:
            sig = sig_map[i]
            entry  = sig["entry"]
            sl_prc = sig["sl"]
            sl_dist = abs(entry - sl_prc)
            if sl_dist < entry * 0.0001:
                continue

            risk_amount = capital * risk_pct
            qty         = risk_amount / sl_dist
            notional    = qty * entry
            margin      = notional / leverage

            if margin > capital * 0.20:
                margin   = capital * 0.20
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
                "qty":         qty,
                "notional":    notional,
                "margin":      margin,
                "leverage":    leverage,
                "entry_fee":   entry_fee,
                "funding_fees": 0.0,
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
    }

    return {"trades": trades, "equity_curve": equity_curve, "stats": stats}


# ════════════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════════════

def full_analysis(
    klines:          List[Dict],
    initial_capital: float = 10_000.0,
    leverage:        int   = 10,
    risk_pct:        float = 0.01,
    interval:        str   = "1h",
    taker_fee:       float = TAKER_FEE,
    funding_map:     Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    closes = [k["close"] for k in klines]
    macd_line, sig_line, hist = compute_macd(closes)

    fractals      = detect_fractals(klines)
    bis           = detect_bi(klines, fractals)
    zhongshu_list = detect_zhongshu(bis)
    signals       = generate_signals(klines, bis, zhongshu_list, hist)
    bt            = run_backtest(
        klines, signals, initial_capital, leverage, risk_pct, interval,
        taker_fee=taker_fee, funding_map=funding_map,
    )

    return {
        # 結構資料（前端繪圖用）
        "fractals": fractals,
        "bis": [
            {
                "start_time":  b["start"]["time"],
                "end_time":    b["end"]["time"],
                "start_price": b["start_price"],
                "end_price":   b["end_price"],
                "direction":   b["direction"],
            }
            for b in bis
        ],
        "zhongshu": [
            {
                "zl":         z["zl"],
                "zh":         z["zh"],
                "start_time": z["start_time"],
                "end_time":   z["end_time"],
            }
            for z in zhongshu_list
        ],
        "macd": {
            "macd_line":   macd_line,
            "signal_line": sig_line,
            "histogram":   hist,
        },
        "signals": signals,
        # 回測結果
        **bt,
    }
