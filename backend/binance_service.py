import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx

BASE_URL = "https://testnet.binancefuture.com"


class BinanceService:
    def __init__(self):
        self.api_key = os.getenv("BINANCE_API_KEY", "")
        self.api_secret = os.getenv("BINANCE_API_SECRET", "")

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    def _check(self, res: httpx.Response):
        if res.status_code >= 400:
            try:
                body = res.json()
                msg = body.get("msg", res.text)
                code = body.get("code", res.status_code)
            except Exception:
                msg = res.text
                code = res.status_code
            raise RuntimeError(f"Binance error {code}: {msg}")

    async def ping(self) -> dict:
        """Diagnostics: clock drift + raw account response."""
        async with httpx.AsyncClient() as client:
            t = await client.get(f"{BASE_URL}/fapi/v1/time", timeout=5)
            server_ms = t.json().get("serverTime", 0)
            local_ms = int(time.time() * 1000)

            params = self._sign({})
            acc = await client.get(
                f"{BASE_URL}/fapi/v2/account",
                params=params,
                headers=self._headers(),
                timeout=10,
            )
            return {
                "server_time": server_ms,
                "local_time": local_ms,
                "clock_drift_ms": local_ms - server_ms,
                "account_status": acc.status_code,
                "account_body": acc.json(),
            }

    async def get_klines(self, symbol: str, interval: str, limit: int = 200):
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{BASE_URL}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
            )
            self._check(res)
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
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{BASE_URL}/fapi/v1/ticker/24hr",
                params={"symbol": symbol},
                timeout=10,
            )
            self._check(res)
            return res.json()

    async def get_account(self):
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{BASE_URL}/fapi/v2/account",
                params=self._sign({}),
                headers=self._headers(),
                timeout=10,
            )
            self._check(res)
            data = res.json()
            return {
                "totalWalletBalance": float(data.get("totalWalletBalance", 0)),
                "totalUnrealizedProfit": float(data.get("totalUnrealizedProfit", 0)),
                "totalMarginBalance": float(data.get("totalMarginBalance", 0)),
                "availableBalance": float(data.get("availableBalance", 0)),
            }

    async def get_positions(self):
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{BASE_URL}/fapi/v2/positionRisk",
                params=self._sign({}),
                headers=self._headers(),
                timeout=10,
            )
            self._check(res)
            positions = []
            for p in res.json():
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                margin = float(p.get("initialMargin", 0))
                pnl = float(p.get("unRealizedProfit", 0))
                positions.append({
                    "symbol": p["symbol"],
                    "positionAmt": amt,
                    "entryPrice": float(p.get("entryPrice", 0)),
                    "unRealizedProfit": pnl,
                    "percentage": (pnl / margin * 100) if margin > 0 else 0,
                    "leverage": int(p.get("leverage", 1)),
                    "markPrice": float(p.get("markPrice", 0)),
                })
            return positions

    async def get_open_orders(self, symbol: str):
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{BASE_URL}/fapi/v1/openOrders",
                params=self._sign({"symbol": symbol}),
                headers=self._headers(),
                timeout=10,
            )
            self._check(res)
            return res.json()

    async def place_order(self, order):
        params: dict = {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "quantity": order.quantity,
        }
        if order.order_type == "LIMIT" and order.price:
            params["price"] = order.price
            params["timeInForce"] = "GTC"

        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"{BASE_URL}/fapi/v1/order",
                params=self._sign(params),
                headers=self._headers(),
                timeout=10,
            )
            return res.json()

    async def cancel_order(self, symbol: str, order_id: int):
        async with httpx.AsyncClient() as client:
            res = await client.delete(
                f"{BASE_URL}/fapi/v1/order",
                params=self._sign({"symbol": symbol, "orderId": order_id}),
                headers=self._headers(),
                timeout=10,
            )
            return res.json()
