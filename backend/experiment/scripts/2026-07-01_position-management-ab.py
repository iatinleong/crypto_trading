"""
持倉管理 A/B 回測腳本（反手開倉 / 保證金上限 / 移動止損 / 組合）。
對應實驗記錄：backend/experiment/2026-07-01_position-management-ab.md

直接讀本地 SQLite 快取（backend/data/market_cache.db），不打 API，
用同一批訊號（generate_signals 的結果）餵給不同參數的 run_backtest()，
確保每組差異只來自持倉管理邏輯本身，不是訊號變了。

執行方式：在 backend/ 目錄下 `python experiment/scripts/2026-07-01_position-management-ab.py`
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from kline_cache import _read_klines_sync, _read_funding_sync
from backtest_engine import (
    handle_inclusion, compute_macd, detect_fractals, detect_bi, detect_duan,
    detect_zhongshu, generate_signals, run_backtest,
)


def load(symbol, interval, limit):
    klines = _read_klines_sync(symbol, interval, limit)
    funding_map = _read_funding_sync(symbol, klines[0]["time"], klines[-1]["time"])
    return klines, funding_map


def build_signals(klines):
    merged_klines = handle_inclusion(klines)
    merged_hist = compute_macd([k["close"] for k in merged_klines])[2]
    fractals = detect_fractals(merged_klines)
    bis = detect_bi(merged_klines, fractals)
    duans = detect_duan(bis)
    zhongshu_list = detect_zhongshu(duans if len(duans) >= 3 else bis)
    return generate_signals(klines, merged_klines, bis, zhongshu_list, merged_hist, False)


def r_multiple(trades):
    """每筆交易的淨損益 ÷ 該筆的風險金額（qty × 進場止損距離），不受資金複利規模影響。"""
    rs = []
    for t in trades:
        risk = t["qty"] * t["sl_dist"]
        if risk > 0:
            rs.append(t["pnl"] / risk)
    return sum(rs) / len(rs) if rs else 0.0


VARIANTS = {
    "baseline（現行：忽略反向訊號，等SL/TP）": dict(),
    "反手開倉": dict(reversal_on_opposite=True),
    "保證金上限5%": dict(margin_cap_pct=0.05),
    "保證金上限2%": dict(margin_cap_pct=0.02),
    "移動止損(啟動1R/跟隨1R，仍有固定TP上限)": dict(
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0),
    "移動止損(啟動0.5R/跟隨0.5R，仍有固定TP上限)": dict(
        trailing_stop=True, trailing_activate_r=0.5, trailing_distance_r=0.5),
    "純移動止損(啟動1R/跟隨1R，無固定TP上限)": dict(
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0,
        trailing_disable_fixed_tp=True),
    "純移動止損(啟動0.5R/跟隨0.5R，無固定TP上限)": dict(
        trailing_stop=True, trailing_activate_r=0.5, trailing_distance_r=0.5,
        trailing_disable_fixed_tp=True),
    "移動止盈(啟動1R/延伸1R，止損仍固定)": dict(
        trailing_tp=True, trailing_tp_activate_r=1.0, trailing_tp_distance_r=1.0),
    "移動止盈(啟動0.5R/延伸0.5R，止損仍固定)": dict(
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=0.5),
    "移動止盈+移動止損(都是1R/1R)": dict(
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0,
        trailing_tp=True, trailing_tp_activate_r=1.0, trailing_tp_distance_r=1.0),
    "移動止盈+移動止損(都是0.5R/0.5R)": dict(
        trailing_stop=True, trailing_activate_r=0.5, trailing_distance_r=0.5,
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=0.5),
    "反手開倉+移動止損(1R/1R，仍有固定TP上限)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0),
    "反手開倉+純移動止損(1R/1R，無固定TP上限)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0,
        trailing_disable_fixed_tp=True),
    "反手開倉+純移動止損(0.5R/0.5R，無固定TP上限)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.5, trailing_distance_r=0.5,
        trailing_disable_fixed_tp=True),
    "反手開倉+移動止盈+移動止損(都是1R/1R)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=1.0, trailing_distance_r=1.0,
        trailing_tp=True, trailing_tp_activate_r=1.0, trailing_tp_distance_r=1.0),
    "反手開倉+移動止盈+移動止損(都是0.5R/0.5R)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.5, trailing_distance_r=0.5,
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=0.5),
    "反手開倉+移動止盈+移動止損(都是0.3R/0.3R)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.3, trailing_distance_r=0.3,
        trailing_tp=True, trailing_tp_activate_r=0.3, trailing_tp_distance_r=0.3),
    "反手開倉+移動止盈+移動止損(都是0.2R/0.2R)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.2, trailing_distance_r=0.2,
        trailing_tp=True, trailing_tp_activate_r=0.2, trailing_tp_distance_r=0.2),

    # ── 窄止損＋寬止盈（反手開倉基礎上）─────────────────────────────────────
    # 假設：只把止損收緊（鎖利更快/少賠），但止盈維持原本結構性目標（自然較寬）
    # 或用移動止盈讓目標往更遠處延伸，是否比對稱式(1R/1R, 0.5R/0.5R)更好？
    "反手開倉+移動止損窄(0.3R/0.3R，仍有固定結構TP)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.3, trailing_distance_r=0.3),
    "反手開倉+移動止損窄(0.2R/0.2R，仍有固定結構TP)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.2, trailing_distance_r=0.2),
    "反手開倉+移動止損窄(0.3R/0.3R)+移動止盈寬(啟動0.5R/延伸2R，無固定TP)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.3, trailing_distance_r=0.3,
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=2.0,
        trailing_disable_fixed_tp=True),
    "反手開倉+移動止損窄(0.3R/0.3R)+移動止盈寬(啟動0.5R/延伸3R，無固定TP)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.3, trailing_distance_r=0.3,
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=3.0,
        trailing_disable_fixed_tp=True),
    "反手開倉+移動止損窄(0.2R/0.2R)+移動止盈寬(啟動0.5R/延伸3R，無固定TP)": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.2, trailing_distance_r=0.2,
        trailing_tp=True, trailing_tp_activate_r=0.5, trailing_tp_distance_r=3.0,
        trailing_disable_fixed_tp=True),

    # ── 真正測試「窄止損＋寬止盈」：放寬固定 TP ─────────────────────────────────────
    "反手開倉+固定TP放大1.5倍": dict(
        reversal_on_opposite=True,
        tp_multiplier=1.5),
    "反手開倉+固定TP放大2.0倍": dict(
        reversal_on_opposite=True,
        tp_multiplier=2.0),
    "反手開倉+固定TP放大3.0倍": dict(
        reversal_on_opposite=True,
        tp_multiplier=3.0),
    "反手開倉+移動止損寬(1.5R/1.5R)+固定TP放大1.5倍": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=1.5, trailing_distance_r=1.5,
        tp_multiplier=1.5),
    "反手開倉+移動止損寬(1.5R/1.5R)+固定TP放大2.0倍": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=1.5, trailing_distance_r=1.5,
        tp_multiplier=2.0),
    "反手開倉+移動止損寬(2.0R/2.0R)+固定TP放大2.0倍": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=2.0, trailing_distance_r=2.0,
        tp_multiplier=2.0),
    "反手開倉+移動止損窄(0.2R/0.2R)+固定TP放大1.5倍": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.2, trailing_distance_r=0.2,
        tp_multiplier=1.5),
    "反手開倉+移動止損窄(0.2R/0.2R)+固定TP放大2.0倍": dict(
        reversal_on_opposite=True,
        trailing_stop=True, trailing_activate_r=0.2, trailing_distance_r=0.2,
        tp_multiplier=2.0),
}


def run(symbol, interval, limit):
    klines, funding_map = load(symbol, interval, limit)
    signals = build_signals(klines)
    print(f"\n=== {symbol}/{interval}  candles={len(klines)}  signals={len(signals)} ===")

    common = dict(
        klines=klines, initial_capital=500.0, leverage=10, risk_pct=0.01,
        interval=interval, taker_fee=0.0005, funding_map=funding_map,
    )

    results = {}
    for name, kwargs in VARIANTS.items():
        run_kwargs = dict(kwargs)
        tp_mult = run_kwargs.pop("tp_multiplier", 1.0)
        
        # 依倍數調整訊號 TP 距離
        test_signals = signals
        if tp_mult != 1.0:
            test_signals = []
            for s in signals:
                new_s = dict(s)
                entry = s["entry"]
                sl = s["sl"]
                tp = s["tp"]
                direction = 1 if s["side"] == "BUY" else -1
                
                # 計算原始 TP 距離並放大
                tp_dist = abs(tp - entry)
                new_tp = entry + direction * (tp_dist * tp_mult)
                new_s["tp"] = new_tp
                test_signals.append(new_s)
                
        r = run_backtest(**{**common, "signals": test_signals}, **run_kwargs)
        s = r["stats"]
        avg_r = r_multiple(r["trades"])
        s["avg_r_per_trade"] = avg_r
        results[name] = s
        print(f"-- {name}")
        print(f"   trades={s['total_trades']} win_rate={s['win_rate']:.1f}% total_return={s['total_return']:.1f}% "
              f"max_dd={s['max_drawdown']:.2f}% pf={s['profit_factor']:.2f} avg_R={avg_r:.3f} "
              f"final_capital={s['final_capital']:.2f}")
    return results


if __name__ == "__main__":
    all_results = {}
    all_results["BTCUSDT/1h"] = run("BTCUSDT", "1h", 30000)
    all_results["BTCUSDT/4h"] = run("BTCUSDT", "4h", 8000)

    out_path = os.path.join(os.path.dirname(__file__), "2026-07-01_position-management-ab.results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {out_path}")
