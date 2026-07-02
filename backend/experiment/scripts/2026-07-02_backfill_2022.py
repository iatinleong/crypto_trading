"""
把本地 K 棒快取（backend/data/market_cache.db）從目前的 2023-07 補回到 2022-01-01，
含資金費率，讓回測範圍能涵蓋 2022 年至今（約4.5年）。

執行方式：在 backend/ 目錄下 `python experiment/scripts/2026-07-02_backfill_2022.py`
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from market import MarketClient
import kline_cache

SYMBOL = "BTCUSDT"
START_TIME = 1640995200  # 2022-01-01 00:00:00 UTC
INTERVALS = {"1h": 41000, "4h": 10500, "1d": 1750}  # total_limit 足以涵蓋到2022年初


async def main():
    m = MarketClient()

    for interval, total_limit in INTERVALS.items():
        klines = await m.get_klines_cached(SYMBOL, interval, total_limit)
        earliest = klines[0]["time"] if klines else None
        latest = klines[-1]["time"] if klines else None
        print(f"[{interval}] cached={len(klines)} "
              f"earliest={time.strftime('%Y-%m-%d', time.gmtime(earliest)) if earliest else '-'} "
              f"latest={time.strftime('%Y-%m-%d', time.gmtime(latest)) if latest else '-'}")

    end_time = int(time.time())
    funding = await m.get_funding_rates_cached(SYMBOL, START_TIME, end_time)
    print(f"[funding] cached={len(funding)} range={time.strftime('%Y-%m-%d', time.gmtime(min(funding)))}"
          f" ~ {time.strftime('%Y-%m-%d', time.gmtime(max(funding)))}" if funding else "[funding] empty")


if __name__ == "__main__":
    asyncio.run(main())
