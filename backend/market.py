import math
import warnings
import httpx

import kline_cache

warnings.filterwarnings("ignore", message=".*Unverified HTTPS.*")

MARKET_URL = "https://fapi.binance.com"

# Windows 有時缺乏中繼憑證，關閉驗證允許連線（Binance 公開行情端點，非帳戶操作）
_CLIENT = dict(verify=False)


class MarketClient:
    async def get_klines(self, symbol: str, interval: str, limit: int = 200):
        async with httpx.AsyncClient(**_CLIENT) as client:
            res = await client.get(
                f"{MARKET_URL}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
            )
            res.raise_for_status()
            return [
                {
                    "time": int(k[0]) // 1000,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
                for k in res.json()
            ]

    async def _fetch_klines_paginated(self, symbol: str, interval: str, total_limit: int,
                                       end_time_ms: int | None = None):
        """
        分頁抓取K棒，從 end_time_ms（不含）往回一批批抓 1500 根，直到累積到
        total_limit 根或資料用盡。end_time_ms=None 代表從現在最新的資料開始抓。
        """
        max_calls = min(300, math.ceil(total_limit / 1500) + 1)
        batches: list[list[dict]] = []
        collected = 0

        async with httpx.AsyncClient(**_CLIENT) as client:
            for _ in range(max_calls):
                params = {"symbol": symbol, "interval": interval, "limit": 1500}
                if end_time_ms is not None:
                    params["endTime"] = end_time_ms
                res = await client.get(f"{MARKET_URL}/fapi/v1/klines", params=params, timeout=15)
                res.raise_for_status()
                raw = res.json()
                if not raw:
                    break
                batch = [
                    {
                        "time": int(k[0]) // 1000,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                    for k in raw
                ]
                batches.append(batch)
                collected += len(batch)
                if collected >= total_limit or len(raw) < 1500:
                    break
                end_time_ms = int(raw[0][0]) - 1   # 下一批抓這批最早那根之前的資料

        batches.reverse()
        merged = [c for b in batches for c in b]
        return merged[-total_limit:] if len(merged) > total_limit else merged

    async def get_klines_history(self, symbol: str, interval: str, total_limit: int):
        """分頁抓取超過單次 1500 根上限的歷史K棒（例如回測一年/五年），從現在往回抓。"""
        return await self._fetch_klines_paginated(symbol, interval, total_limit)

    async def get_klines_cached(self, symbol: str, interval: str, total_limit: int):
        """
        本地 SQLite 快取版本：已收盤的舊K棒不重複打 Binance，只補缺的舊資料；
        最新幾根（可能還在變動）一律重新抓取覆蓋。
        """
        cached_count, earliest = await kline_cache.count_and_earliest(symbol, interval)
        missing = total_limit - cached_count
        if missing > 0:
            end_time_ms = (earliest * 1000 - 1) if earliest else None
            older = await self._fetch_klines_paginated(symbol, interval, missing, end_time_ms)
            await kline_cache.upsert_klines(symbol, interval, older)

        fresh_tail = await self.get_klines(symbol, interval, min(3, total_limit))
        await kline_cache.upsert_klines(symbol, interval, fresh_tail)

        return await kline_cache.read_klines(symbol, interval, total_limit)

    async def get_ticker(self, symbol: str):
        async with httpx.AsyncClient(**_CLIENT) as client:
            res = await client.get(
                f"{MARKET_URL}/fapi/v1/ticker/24hr",
                params={"symbol": symbol},
                timeout=10,
            )
            res.raise_for_status()
            return res.json()

    async def get_price(self, symbol: str) -> float:
        async with httpx.AsyncClient(**_CLIENT) as client:
            res = await client.get(
                f"{MARKET_URL}/fapi/v1/ticker/price",
                params={"symbol": symbol},
                timeout=5,
            )
            res.raise_for_status()
            return float(res.json()["price"])

    async def get_current_funding_rate(self, symbol: str) -> dict:
        """
        取得目前（下一次結算前）的預測資金費率與標記價格。
        Binance 在結算當下即以這個 lastFundingRate 入帳。
        """
        async with httpx.AsyncClient(**_CLIENT) as client:
            res = await client.get(
                f"{MARKET_URL}/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()
            return {
                "mark_price": float(data["markPrice"]),
                "funding_rate": float(data["lastFundingRate"]),
                "next_funding_time": int(data["nextFundingTime"]) // 1000,
            }

    async def get_funding_rates(self, symbol: str, start_time: int, end_time: int) -> dict[int, float]:
        """
        取得 [start_time, end_time] 範圍內的歷史資金費率。
        回傳 {settlement_unix_sec: rate} dict，Binance 每 8h 一筆。
        """
        rates: dict[int, float] = {}
        # Binance 每次最多 1000 筆，視範圍可能需分頁
        cursor = start_time * 1000  # ms
        async with httpx.AsyncClient(**_CLIENT) as client:
            while cursor < end_time * 1000:
                res = await client.get(
                    f"{MARKET_URL}/fapi/v1/fundingRate",
                    params={"symbol": symbol, "startTime": cursor, "limit": 1000},
                    timeout=10,
                )
                res.raise_for_status()
                data = res.json()
                if not data:
                    break
                for item in data:
                    ts_sec = int(item["fundingTime"]) // 1000
                    rates[ts_sec] = float(item["fundingRate"])
                last_ts = int(data[-1]["fundingTime"])
                if len(data) < 1000:
                    break
                cursor = last_ts + 1
        return rates

    async def get_funding_rates_cached(self, symbol: str, start_time: int, end_time: int) -> dict[int, float]:
        """
        本地快取版本：資金費一旦結算就不會再變，覆蓋率明顯足夠時直接用快取，
        否則整段重新抓取並存回快取（資金費每 8h 一筆，重抓成本很低）。
        """
        cached = await kline_cache.read_funding(symbol, start_time, end_time)
        expected = max(1, (end_time - start_time) // (8 * 3600))
        if len(cached) < expected * 0.9:
            fresh = await self.get_funding_rates(symbol, start_time, end_time)
            if fresh:
                await kline_cache.upsert_funding(symbol, fresh)
                cached = {**cached, **fresh}
        return cached
