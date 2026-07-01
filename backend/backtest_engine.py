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
# K線包含關係處理 (Inclusion)
# ════════════════════════════════════════════════════════════════════════════

def handle_inclusion(klines: List[Dict]) -> List[Dict]:
    """
    若 Kᵢ 與 Kᵢ₊₁ 存在包含關係（一根的高低點完全包住另一根），依當前趨勢方向合併：
    向上取高高、低高（max/max）；向下取低低、高低（min/min）。合併後K線繼承較新
    那根的 time，並記錄 raw_index = 該合併結果最後吞入的原始K棒 index，供下游
    需要「回到原始K線」的邏輯（精確進場價、B3/S3 原始突破掃描）換算座標。
    """
    if not klines:
        return []
    result: List[Dict] = [{**klines[0], "raw_index": 0}]
    direction = 0   # 1=向上, -1=向下；尚未確立趨勢時預設視為向上

    for i in range(1, len(klines)):
        cur = klines[i]
        last = result[-1]
        contains = (last["high"] >= cur["high"] and last["low"] <= cur["low"]) or \
                   (last["high"] <= cur["high"] and last["low"] >= cur["low"])

        if contains:
            if direction >= 0:
                new_high, new_low = max(last["high"], cur["high"]), max(last["low"], cur["low"])
            else:
                new_high, new_low = min(last["high"], cur["high"]), min(last["low"], cur["low"])
            result[-1] = {
                **last,
                "high": new_high, "low": new_low,
                "close": cur["close"], "time": cur["time"],
                "raw_index": i,
            }
        else:
            direction = 1 if cur["high"] > last["high"] else -1
            result.append({**cur, "raw_index": i})

    return result


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


def _merge_fractals(fractals: List[Dict], min_gap: int = 4) -> List[Dict]:
    """
    分型合併，確保輸出嚴格交替、相鄰間距 >= min_gap 根K棒：
    1) 同方向分型視為同一波段的noise，只留最極端的一個。
    2) 相鄰異方向分型間距不足 min_gap，視為無效分型直接剔除——剔除後，
       原本被隔開的前後同方向分型會變成相鄰，下一輪迴圈會自動比較取極值，
       等同於反覆合併到序列穩定（單一線性掃描即可完成，不需多次遍歷）。
    這樣才能保證後面用「相鄰分型逐對相接」建笔時，方向必定交替、首尾相接。
    """
    if not fractals:
        return []
    result = [fractals[0]]
    for f in fractals[1:]:
        last = result[-1]
        if f["type"] == last["type"]:
            if (f["type"] == "top"    and f["price"] > last["price"]) or \
               (f["type"] == "bottom" and f["price"] < last["price"]):
                result[-1] = f
        elif f["index"] - last["index"] < min_gap:
            continue   # 間距不足，剔除；下一個分型會跟 last 比較（可能觸發極值合併）
        else:
            result.append(f)
    return result


# ════════════════════════════════════════════════════════════════════════════
# 笔 (Bi)
# ════════════════════════════════════════════════════════════════════════════

def detect_bi(klines: List[Dict], fractals: List[Dict]) -> List[Dict]:
    """
    有效笔：由 _merge_fractals 處理後嚴格交替、間距足夠的分型序列，
    逐對相鄰分型首尾相接構成（保證方向交替、無重疊）。
    """
    merged = _merge_fractals(fractals)
    bis = []
    for i in range(len(merged) - 1):
        f1, f2 = merged[i], merged[i+1]
        direction = "up" if f2["type"] == "top" else "down"
        bis.append({
            "start": f1, "end": f2,
            "direction": direction,
            "start_price": f1["price"],
            "end_price":   f2["price"],
        })
    return bis


# ════════════════════════════════════════════════════════════════════════════
# 線段 (Xianduan / Segment) — 特徵序列法
# ════════════════════════════════════════════════════════════════════════════

def _merge_feature_seq(seq_bis: List[Dict]) -> List[Dict]:
    """
    對特征序列（一串同方向的笔，各笔視為一根「K線」）做包含處理，
    回傳標準特征序列。每個元素同時記錄「高點來自哪一笔」「低點來自哪一笔」，
    因為合併時高低點可能各自來自不同的原始笔，不能只記一個代表笔。
    """
    def hilo(b: Dict) -> Tuple[float, float]:
        return max(b["start_price"], b["end_price"]), min(b["start_price"], b["end_price"])

    if not seq_bis:
        return []
    up = seq_bis[0]["direction"] == "up"
    h0, l0 = hilo(seq_bis[0])
    result = [{"high": h0, "low": l0, "hi_bi": seq_bis[0], "lo_bi": seq_bis[0]}]

    for bi in seq_bis[1:]:
        hi, lo = hilo(bi)
        last = result[-1]
        contains = (last["high"] >= hi and last["low"] <= lo) or (last["high"] <= hi and last["low"] >= lo)
        if contains:
            if up:
                new_hi, new_hi_bi = (last["high"], last["hi_bi"]) if last["high"] >= hi else (hi, bi)
                new_lo, new_lo_bi = (last["low"],  last["lo_bi"]) if last["low"]  >= lo else (lo, bi)
            else:
                new_hi, new_hi_bi = (last["high"], last["hi_bi"]) if last["high"] <= hi else (hi, bi)
                new_lo, new_lo_bi = (last["low"],  last["lo_bi"]) if last["low"]  <= lo else (lo, bi)
            result[-1] = {"high": new_hi, "low": new_lo, "hi_bi": new_hi_bi, "lo_bi": new_lo_bi}
        else:
            result.append({"high": hi, "low": lo, "hi_bi": bi, "lo_bi": bi})
    return result


def _feature_fractal_index(seq: List[Dict], want: str) -> Optional[int]:
    """在標準特征序列裡找第一個符合 want('top'/'bottom') 的分型，回傳中間元素 index。"""
    for i in range(1, len(seq) - 1):
        p, c, n = seq[i-1], seq[i], seq[i+1]
        if want == "top" and p["high"] < c["high"] and n["high"] < c["high"]:
            return i
        if want == "bottom" and p["low"] > c["low"] and n["low"] > c["low"]:
            return i
    return None


def detect_duan(bis: List[Dict]) -> List[Dict]:
    """
    線段劃分（特征序列法）：
    - 線段方向由其第一笔決定；只收集線段內「反方向」的笔組成特征序列。
    - 特征序列做包含處理成標準特征序列，出現對應分型（上升段找頂分型、
      下降段找底分型）即為線段結束的候選點，結束點是該分型峰值所屬的那一笔的起點。
    - 簡化：纏論原著對「特征序列缺口」有更複雜的延遲確認規則（缺口案例本身在
      纏論社群裡就頗具爭議、不同實作各有版本），這裡不處理缺口延遲，一律以
      「找到分型即確認」處理——這是常見、可接受的工程簡化，非嚴格原著定義。
    """
    if len(bis) < 3:
        return []

    duans: List[Dict] = []
    seg_start = 0
    direction = bis[0]["direction"]
    i = 2

    while i < len(bis):
        opp = "down" if direction == "up" else "up"
        feature_bis = [b for b in bis[seg_start:i+1] if b["direction"] == opp]
        if len(feature_bis) >= 3:
            std_seq = _merge_feature_seq(feature_bis)
            want = "top" if direction == "up" else "bottom"
            fi = _feature_fractal_index(std_seq, want)
            if fi is not None:
                end_bi = std_seq[fi]["hi_bi"] if want == "top" else std_seq[fi]["lo_bi"]
                end_pos = next(k for k in range(seg_start, i + 1) if bis[k] is end_bi)
                duans.append({
                    "start": {"time": bis[seg_start]["start"]["time"], "index": bis[seg_start]["start"]["index"]},
                    "end":   {"time": end_bi["start"]["time"], "index": end_bi["start"]["index"]},
                    "direction":   direction,
                    "start_price": bis[seg_start]["start_price"],
                    "end_price":   end_bi["start_price"],
                })
                seg_start = end_pos
                direction = opp
                i = seg_start + 2
                continue
        i += 1

    return duans


# ════════════════════════════════════════════════════════════════════════════
# 中枢 (Zhongshu / Pivot Zone)
# ════════════════════════════════════════════════════════════════════════════

def detect_zhongshu(bis: List[Dict]) -> List[Dict]:
    """
    三筆重疊形成中枢核心：ZL = max(各笔低點), ZH = min(各笔高點), 且 ZH > ZL。
    核心一旦形成即固定不變，後續笔只要仍與這個固定核心重疊就算延伸；
    一旦某笔完全不重疊（突破），該中枢結束，下一個中枢從突破笔開始重新尋找核心。

    採「往前走、互不重疊」而非「全域滑動視窗＋事後聯集合併」，
    避免中枢區間隨延伸不斷取 min/max 聯集而被無限拉寬。
    """
    def bi_range(b: Dict) -> Tuple[float, float]:
        lo = min(b["start_price"], b["end_price"])
        hi = max(b["start_price"], b["end_price"])
        return lo, hi

    result: List[Dict] = []
    n = len(bis)
    i = 0
    while i <= n - 3:
        b1, b2, b3 = bis[i], bis[i+1], bis[i+2]
        r1, r2, r3 = bi_range(b1), bi_range(b2), bi_range(b3)
        zl = max(r1[0], r2[0], r3[0])
        zh = min(r1[1], r2[1], r3[1])
        if zh <= zl:
            i += 1   # 三笔不重疊，往前移一笔重新嘗試核心
            continue

        zs: Dict[str, Any] = {
            "zl": zl, "zh": zh,
            "start_time":  b1["start"]["time"],
            "end_time":    b3["end"]["time"],
            "start_index": b1["start"]["index"],
            "end_index":   b3["end"]["index"],
            "entry_dir":   b1["direction"],
        }

        # 延伸中枢：後續笔仍與固定核心 [zl, zh] 重疊則併入（核心本身不變）
        j = i + 3
        while j < n:
            lo_j, hi_j = bi_range(bis[j])
            if lo_j <= zh and hi_j >= zl:
                zs["end_time"]  = bis[j]["end"]["time"]
                zs["end_index"] = bis[j]["end"]["index"]
                j += 1
            else:
                break

        result.append(zs)
        i = j   # 從突破的那笔開始找下一個中枢，確保中枢之間不重疊、不聯集

    return result


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
    raw_klines:    List[Dict],
    merged_klines: List[Dict],
    bis:           List[Dict],
    zhongshu_list: List[Dict],
    hist:          List[Optional[float]],
) -> List[Dict]:
    """
    hist 是在 merged_klines 上算的 MACD 柱狀圖（跟 bis/zhongshu_list 同屬「合併後」
    index 空間），只用於背驰面積比較。實際決定進場價格/時間、以及 B3/S3
    的突破/回踩掃描，一律換算回 raw_klines 的座標（透過 to_raw()），
    因為合併後一根K線可能吞掉好幾根真實K棒，不能拿來當精確進場時機。
    """
    signals: List[Dict] = []
    n = len(raw_klines)

    def entry_at(raw_idx: int) -> Tuple[float, int, int]:
        """下根原始K棒開盤作為入場。"""
        ni = min(raw_idx + 1, n - 1)
        return raw_klines[ni]["open"], ni, raw_klines[ni]["time"]

    def to_raw(merged_idx: int) -> int:
        return merged_klines[merged_idx]["raw_index"]

    # ── B1 / S1：背驰买卖点 ────────────────────────────────────────────────
    first_point_bi: Dict[int, str] = {}   # bi index -> "B1"/"S1"，供下面 B2/S2 使用
    for i, bi in enumerate(bis):
        end_idx = bi["end"]["index"]        # 合併後 index（給中枢比較用）
        raw_end_idx = to_raw(end_idx)        # 原始 index（給進場/邊界判斷用）
        if raw_end_idx >= n - 2:
            continue
        prev = _prev_same_dir(bis, i)
        if prev is None:
            continue

        curr_area = _macd_area(bi,   hist)
        prev_area = _macd_area(prev, hist)
        if prev_area < 1e-9:
            continue

        divergence = curr_area < prev_area * 0.85  # 至少弱 15%

        entry, ei, et = entry_at(raw_end_idx)

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
            first_point_bi[i] = "B1"

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
            first_point_bi[i] = "S1"

    # ── B2 / S2：第一类买卖点後，次級别回調不創新低/新高 ─────────────────────
    # B2：bi[i]是B1（向下笔）；bi[i+1]是次級别反彈（向上，笔交替保證方向）；
    #     bi[i+2]再次下跌，若這次的低點沒有跌破bi[i]的低點，該笔結束點即為B2。
    # S2 為鏡像（bi[i]是S1，bi[i+2]反彈不創新高）。
    for i, sig_type in first_point_bi.items():
        if i + 2 >= len(bis):
            continue
        b0, b2_bi = bis[i], bis[i + 2]
        raw_end_idx = to_raw(b2_bi["end"]["index"])
        if raw_end_idx >= n - 2:
            continue
        entry, ei, et = entry_at(raw_end_idx)

        if sig_type == "B1" and b2_bi["end_price"] > b0["end_price"]:
            sl = round(b2_bi["end_price"] * 0.9970, 2)
            tp_raw = b2_bi["start_price"]
            zs_res = [z["zh"] for z in zhongshu_list
                      if z["zh"] > entry and z["start_index"] < b2_bi["end"]["index"]]
            tp = min(zs_res) if zs_res else tp_raw
            if tp <= entry:
                tp = round(entry + (entry - sl) * 2, 4)
            signals.append({
                "index": ei, "time": et,
                "type": "B2", "side": "BUY",
                "entry": entry, "sl": sl, "tp": tp,
                "reason": "第二类买点：B1後次級别回調不創新低",
                "fractal_price": b2_bi["end_price"], "fractal_time": b2_bi["end"]["time"],
            })
        elif sig_type == "S1" and b2_bi["end_price"] < b0["end_price"]:
            sl = round(b2_bi["end_price"] * 1.0030, 2)
            tp_raw = b2_bi["start_price"]
            zs_sup = [z["zl"] for z in zhongshu_list
                      if z["zl"] < entry and z["start_index"] < b2_bi["end"]["index"]]
            tp = max(zs_sup) if zs_sup else tp_raw
            if tp >= entry:
                tp = round(entry - (sl - entry) * 2, 4)
            signals.append({
                "index": ei, "time": et,
                "type": "S2", "side": "SELL",
                "entry": entry, "sl": sl, "tp": tp,
                "reason": "第二类卖点：S1後次級别反彈不創新高",
                "fractal_price": b2_bi["end_price"], "fractal_time": b2_bi["end"]["time"],
            })

    # ── B3 / S3：中枢突破回踩（一律在原始K棒上掃描，確保捉到真實突破/回踩時刻）───
    used: set = set()
    for zs in zhongshu_list:
        if zs["zh"] <= zs["zl"]:
            continue
        zs_end = to_raw(zs["end_index"])
        search_end = min(zs_end + 60, n - 2)
        broke_up = broke_dn = False

        for j in range(zs_end, search_end):
            k = raw_klines[j]

            if not broke_up and k["close"] > zs["zh"]:
                broke_up = True
                for k2i in range(j + 1, min(j + 40, n - 1)):
                    k2 = raw_klines[k2i]
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
                    k2 = raw_klines[k2i]
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
# 進場條件檢查清單 (Entry Condition Checklist)
# ════════════════════════════════════════════════════════════════════════════

def evaluate_conditions(
    raw_klines:    List[Dict],
    merged_klines: List[Dict],
    bis:           List[Dict],
    zhongshu_list: List[Dict],
    hist:          List[Optional[float]],
    signals:       List[Dict],
) -> Dict[str, Any]:
    """
    評估「目前最新候選笔/中枢」的進場條件清單——不是產生確認訊號，是給前端
    做「條件滿足亮燈、不滿足暗著」用的即時進度視圖。
    B1/S1（背驰）看最後一笔：方向/分型類型是即時已知的事實，只有 MACD 背驰
    強度是連續數值，能顯示「目前弱化多少%、還差多少」。
    B3/S3（中枢突破回踩）看最後一個中枢：突破跟回踩是真正有時間先後、會等待
    的階段，各自獨立判斷「向上突破」跟「向下突破」兩條路徑目前走到哪一步。

    「條件結構上滿足」跟「真的會觸發進場」不是同一件事：
    1) generate_signals() 對「太靠近資料尾端」的笔/中枢事件會直接跳過不產生訊號
       （需要至少2根K棒的緩衝，避免用還在變動中的資料當進場依據）；
    2) 就算真的生成了訊號，也可能是好幾天前的舊訊號，只是後面沒有更新的結構
       蓋掉它。
    這裡不重新發明一套獨立的新鮮度判斷，而是直接查「這個類型的訊號，在真正
    的 signals 列表裡最後一次出現、且落在最近2根K棒內」——跟 strategy_loop
    實際下單用的是同一個判斷依據，checklist 顯示的「可進場」才不會跟真實下
    單行為互相矛盾。
    """
    result: Dict[str, Any] = {"B1": None, "S1": None, "B3": None, "S3": None}
    n = len(raw_klines)
    fresh_cutoff = n - 2   # 跟 strategy_loop 的 `latest["index"] < len(klines)-2` 同一個門檻

    def to_raw(merged_idx: int) -> int:
        return merged_klines[merged_idx]["raw_index"]

    def is_actionable(sig_type: str) -> bool:
        matches = [s for s in signals if s["type"] == sig_type]
        return bool(matches) and matches[-1]["index"] >= fresh_cutoff

    # ── B1 / S1：看最後一笔 ──────────────────────────────────────────────
    if bis:
        last_bi = bis[-1]
        prev = _prev_same_dir(bis, len(bis) - 1)
        is_b1 = last_bi["direction"] == "down"
        sig_type = "B1" if is_b1 else "S1"
        want_frac = "bottom" if is_b1 else "top"

        conditions = [
            {"label": f"笔方向{'向下' if is_b1 else '向上'}", "met": True},
            {"label": f"笔結束於{'底' if is_b1 else '頂'}分型", "met": last_bi["end"]["type"] == want_frac},
            {"label": "有前一笔可比較背驰", "met": prev is not None},
        ]
        if prev is not None:
            curr_area = _macd_area(last_bi, hist)
            prev_area = _macd_area(prev, hist)
            if prev_area > 1e-9:
                weaken_pct = (1 - curr_area / prev_area) * 100
                conditions.append({
                    "label": f"MACD背驰強度 {weaken_pct:.1f}%（需 ≥15%）",
                    "met": weaken_pct >= 15,
                })
            else:
                conditions.append({"label": "前一笔MACD面積有效", "met": False})
        else:
            conditions.append({"label": "MACD背驰強度（需 ≥15%）", "met": False})

        all_met = all(c["met"] for c in conditions)
        result[sig_type] = {
            "all_met":     all_met,
            "actionable":  is_actionable(sig_type),
            "conditions":  conditions,
            "bi_end_time": last_bi["end"]["time"],
        }

    # ── B3 / S3：看最後一個中枢，向上/向下突破各自獨立判斷 ────────────────
    if zhongshu_list:
        zs = zhongshu_list[-1]
        raw_zs_end = to_raw(zs["end_index"])
        n = len(raw_klines)
        search_end = min(raw_zs_end + 60, n - 2)

        for side, sig_type, label_edge in (("up", "B3", "上緣ZH"), ("down", "S3", "下緣ZL")):
            broke = retested = False
            broke_at = retest_at = None
            for j in range(raw_zs_end, search_end):
                k = raw_klines[j]
                crossed = k["close"] > zs["zh"] if side == "up" else k["close"] < zs["zl"]
                if not crossed:
                    continue
                broke = True
                broke_at = k["time"]
                for k2i in range(j + 1, min(j + 40, n - 1)):
                    k2 = raw_klines[k2i]
                    if side == "up":
                        hit = k2["low"] < zs["zh"] * 1.003 and k2["close"] > zs["zh"] * 0.997
                    else:
                        hit = k2["high"] > zs["zl"] * 0.997 and k2["close"] < zs["zl"] * 1.003
                    if hit:
                        retested = True
                        retest_at = k2["time"]
                        break
                break

            conditions = [
                {"label": "存在有效中枢", "met": True},
                {"label": f"收盤突破中枢{label_edge}", "met": broke},
                {"label": "突破後回踩確認", "met": retested},
            ]
            all_met = all(c["met"] for c in conditions)
            result[sig_type] = {
                "all_met":    all_met,
                "actionable": is_actionable(sig_type),
                "conditions": conditions,
                "zs_zl": zs["zl"], "zs_zh": zs["zh"],
                "broke_at": broke_at, "retest_at": retest_at,
            }

    return result


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
# 主入口
# ════════════════════════════════════════════════════════════════════════════

def full_analysis(
    klines:          List[Dict],
    initial_capital: float = 500.0,
    leverage:        int   = 10,
    risk_pct:        float = 0.01,
    interval:        str   = "1h",
    taker_fee:       float = TAKER_FEE,
    funding_map:     Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    # 前端K棒圖 / MACD副圖一律顯示原始K棒，跟回測執行（進場、SL/TP、資金費）用同一份資料
    closes = [k["close"] for k in klines]
    macd_line, sig_line, hist = compute_macd(closes)

    # 缠論結構判斷（分型/笔/中枢/背驰）在包含關係處理後的K線上進行，
    # 避免震盪期間的雜訊分型污染結構；merged_hist 只用於背驰面積比較
    merged_klines = handle_inclusion(klines)
    merged_hist   = compute_macd([k["close"] for k in merged_klines])[2]

    fractals      = detect_fractals(merged_klines)
    bis           = detect_bi(merged_klines, fractals)
    duans         = detect_duan(bis)
    # 中枢由線段構造（纏論原著定義），不是直接由笔構造；背驰仍在笔級別判斷（B1/S1
    # 是較快、較細的訊號），兩者是纏論裡並存但不同顆粒度的合法訊號類型
    zhongshu_list = detect_zhongshu(duans if len(duans) >= 3 else bis)
    signals       = generate_signals(klines, merged_klines, bis, zhongshu_list, merged_hist)
    conditions    = evaluate_conditions(klines, merged_klines, bis, zhongshu_list, merged_hist, signals)
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
        "duans": [
            {
                "start_time":  d["start"]["time"],
                "end_time":    d["end"]["time"],
                "start_price": d["start_price"],
                "end_price":   d["end_price"],
                "direction":   d["direction"],
            }
            for d in duans
        ],
        "macd": {
            "macd_line":   macd_line,
            "signal_line": sig_line,
            "histogram":   hist,
        },
        "signals": signals,
        "conditions": conditions,
        # 回測結果
        **bt,
    }
