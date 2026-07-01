import time

INITIAL_BALANCE = 10_000.0


class PaperEngine:
    def __init__(self, state: dict | None = None):
        if state:
            self.balance: float = state["balance"]
            self.positions: dict = state["positions"]
            self.orders: list = state["orders"]
            self._counter: int = state.get("counter", 1)
        else:
            self.balance = INITIAL_BALANCE
            self.positions = {}
            self.orders = []
            self._counter = 1

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "positions": self.positions,
            "orders": self.orders,
            "counter": self._counter,
        }

    def _next_id(self) -> int:
        oid = self._counter
        self._counter += 1
        return oid

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None = None,
        leverage: int = 10,
        current_price: float | None = None,
    ) -> dict:
        order = {
            "orderId": self._next_id(),
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "origQty": str(quantity),
            "price": str(price or 0),
            "status": "NEW",
            "time": int(time.time() * 1000),
            "leverage": leverage,
        }
        if order_type == "MARKET":
            if current_price is None:
                raise ValueError("No current price available for market order")
            self._fill(symbol, side, quantity, current_price, leverage)
            order["status"] = "FILLED"
            order["avgPrice"] = str(current_price)
        else:
            if price is None:
                raise ValueError("Price required for limit order")
            self.orders.append(order)
        return order

    def cancel_order(self, order_id: int) -> dict:
        for i, o in enumerate(self.orders):
            if o["orderId"] == order_id:
                return self.orders.pop(i)
        raise ValueError(f"Order {order_id} not found")

    def check_limit_orders(self, symbol: str, price: float) -> list:
        filled, remaining = [], []
        for o in self.orders:
            if o["symbol"] != symbol or o["status"] != "NEW":
                remaining.append(o)
                continue
            limit = float(o["price"])
            triggered = (o["side"] == "BUY" and price <= limit) or \
                        (o["side"] == "SELL" and price >= limit)
            if triggered:
                try:
                    self._fill(symbol, o["side"], float(o["origQty"]), limit, o.get("leverage", 10))
                    o["status"] = "FILLED"
                    o["avgPrice"] = str(limit)
                except Exception as e:
                    o["status"] = "REJECTED"
                    o["rejectReason"] = str(e)
                filled.append(o)
            else:
                remaining.append(o)
        self.orders = remaining
        return filled

    def _fill(self, symbol: str, side: str, qty: float, price: float, leverage: int):
        delta = qty if side == "BUY" else -qty
        pos = self.positions.get(symbol)

        if pos is None:
            margin = (qty * price) / leverage
            if self.balance < margin:
                raise ValueError(f"餘額不足 (需要 {margin:.2f}，剩餘 {self.balance:.2f})")
            self.balance -= margin
            self.positions[symbol] = {"amt": delta, "avg_price": price, "margin": margin, "leverage": leverage}
            return

        cur = pos["amt"]
        new_amt = cur + delta

        if (cur > 0 and delta > 0) or (cur < 0 and delta < 0):
            # 加倉
            margin = (qty * price) / leverage
            if self.balance < margin:
                raise ValueError(f"餘額不足 (需要 {margin:.2f}，剩餘 {self.balance:.2f})")
            self.balance -= margin
            total = abs(cur) + qty
            new_avg = (abs(cur) * pos["avg_price"] + qty * price) / total
            self.positions[symbol] = {"amt": new_amt, "avg_price": new_avg, "margin": pos["margin"] + margin, "leverage": leverage}
        else:
            # 減倉 / 平倉 / 反向
            close_qty = min(qty, abs(cur))
            pnl = close_qty * (price - pos["avg_price"]) * (1 if cur > 0 else -1)
            ratio = close_qty / abs(cur)
            self.balance += pos["margin"] * ratio + pnl

            extra = qty - close_qty
            if abs(new_amt) < 1e-9:
                self.positions.pop(symbol, None)
            elif extra > 1e-9:
                new_margin = (extra * price) / leverage
                if self.balance < new_margin:
                    raise ValueError(f"餘額不足進行反向開倉")
                self.balance -= new_margin
                self.positions[symbol] = {"amt": new_amt, "avg_price": price, "margin": new_margin, "leverage": leverage}
            else:
                self.positions[symbol] = {"amt": new_amt, "avg_price": pos["avg_price"], "margin": pos["margin"] * (1 - ratio), "leverage": pos["leverage"]}

    def get_open_orders(self, symbol: str | None = None) -> list:
        return [o for o in self.orders if o["status"] == "NEW" and (symbol is None or o["symbol"] == symbol)]

    def get_positions(self, prices: dict | None = None) -> list:
        result = []
        for sym, pos in self.positions.items():
            mark = (prices or {}).get(sym, pos["avg_price"])
            amt = pos["amt"]
            pnl = abs(amt) * (mark - pos["avg_price"]) * (1 if amt > 0 else -1)
            margin = pos["margin"]
            result.append({
                "symbol": sym,
                "positionAmt": amt,
                "entryPrice": pos["avg_price"],
                "markPrice": mark,
                "unRealizedProfit": pnl,
                "percentage": (pnl / margin * 100) if margin > 0 else 0,
                "leverage": pos["leverage"],
                "margin": margin,
            })
        return result

    def get_account(self, prices: dict | None = None) -> dict:
        positions = self.get_positions(prices)
        total_pnl = sum(p["unRealizedProfit"] for p in positions)
        total_margin = sum(p["margin"] for p in positions)
        wallet = self.balance + total_margin
        return {
            "totalWalletBalance": wallet,
            "totalUnrealizedProfit": total_pnl,
            "totalMarginBalance": wallet + total_pnl,
            "availableBalance": self.balance,
        }
