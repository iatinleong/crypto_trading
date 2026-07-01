import warnings
import httpx

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
