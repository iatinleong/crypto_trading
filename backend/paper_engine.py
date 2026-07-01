import time

INITIAL_BALANCE = 500.0
TAKER_FEE = 0.0005   # 0.05% — MARKET 單（VIP0）
MAKER_FEE = 0.0002   # 0.02% — LIMIT 單成交（VIP0）
FUNDING_INTERVAL_SEC = 8 * 3600
MAINTENANCE_MARGIN_RATE = 0.005   # 簡化維持保證金率；Binance 實際依倉位分級表，此處用固定值近似


class PaperEngine:
    def __init__(self, state: dict | None = None):
        if state:
            self.balance: float = state["balance"]
            self.positions: dict = state["positions"]
            self.orders: list = state["orders"]
            self._counter: int = state.get("counter", 1)
            self.total_fees: float = state.get("total_fees", 0.0)
            self.last_funding_bucket: int = state.get(
                "last_funding_bucket", int(time.time()) // FUNDING_INTERVAL_SEC
            )
            self.trade_history: list = state.get("trade_history", [])
            self.entry_log: list = state.get("entry_log", [])
        else:
            self.balance = INITIAL_BALANCE
            self.positions = {}
            self.orders = []
            self._counter = 1
            self.total_fees = 0.0
            self.last_funding_bucket = int(time.time()) // FUNDING_INTERVAL_SEC
            self.trade_history = []
            self.entry_log = []

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "positions": self.positions,
            "orders": self.orders,
            "counter": self._counter,
            "total_fees": self.total_fees,
            "last_funding_bucket": self.last_funding_bucket,
            "trade_history": self.trade_history,
            "entry_log": self.entry_log,
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
        sl: float | None = None,
        tp: float | None = None,
        source: str = "MANUAL",
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
            "sl": sl,
            "tp": tp,
            "source": source,
        }
        if order_type == "MARKET":
            if current_price is None:
                raise ValueError("No current price available for market order")
            self._fill(symbol, side, quantity, current_price, leverage, TAKER_FEE, sl, tp, source=source)
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
                    self._fill(symbol, o["side"], float(o["origQty"]), limit, o.get("leverage", 10),
                               MAKER_FEE, o.get("sl"), o.get("tp"), source=o.get("source", "MANUAL"))
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

    def _log_entry(self, symbol: str, side: str, qty: float, price: float, leverage: int,
                    sl: float | None, tp: float | None, source: str, now: int) -> None:
        self.entry_log.append({
            "symbol": symbol, "side": side, "qty": qty, "price": price,
            "leverage": leverage, "sl": sl, "tp": tp, "source": source, "time": now,
        })

    def _fill(self, symbol: str, side: str, qty: float, price: float, leverage: int,
              fee_rate: float = TAKER_FEE, sl: float | None = None, tp: float | None = None,
              reason: str = "MANUAL", source: str = "MANUAL"):
        fee = qty * price * fee_rate
        delta = qty if side == "BUY" else -qty
        pos = self.positions.get(symbol)
        now = int(time.time())

        if pos is None:
            margin = (qty * price) / leverage
            if self.balance < margin + fee:
                raise ValueError(f"餘額不足 (需要 {margin + fee:.2f}，剩餘 {self.balance:.2f})")
            self.balance -= (margin + fee)
            self.total_fees += fee
            self.positions[symbol] = {"amt": delta, "avg_price": price, "margin": margin,
                                       "leverage": leverage, "sl": sl, "tp": tp, "entry_time": now}
            self._log_entry(symbol, side, qty, price, leverage, sl, tp, source, now)
            return

        cur = pos["amt"]
        new_amt = cur + delta

        if (cur > 0 and delta > 0) or (cur < 0 and delta < 0):
            # 加倉
            margin = (qty * price) / leverage
            if self.balance < margin + fee:
                raise ValueError(f"餘額不足 (需要 {margin + fee:.2f}，剩餘 {self.balance:.2f})")
            self.balance -= (margin + fee)
            self.total_fees += fee
            total = abs(cur) + qty
            new_avg = (abs(cur) * pos["avg_price"] + qty * price) / total
            self.positions[symbol] = {
                "amt": new_amt, "avg_price": new_avg, "margin": pos["margin"] + margin, "leverage": leverage,
                "sl": sl if sl is not None else pos.get("sl"),
                "tp": tp if tp is not None else pos.get("tp"),
                "entry_time": pos.get("entry_time", now),
            }
            self._log_entry(symbol, side, qty, price, leverage, sl, tp, source, now)
        else:
            # 減倉 / 平倉 / 反向
            close_qty = min(qty, abs(cur))
            pnl = close_qty * (price - pos["avg_price"]) * (1 if cur > 0 else -1)
            ratio = close_qty / abs(cur)
            self.balance += pos["margin"] * ratio + pnl - fee
            self.total_fees += fee

            self.trade_history.append({
                "symbol":      symbol,
                "side":        "BUY" if cur > 0 else "SELL",
                "entry_price": pos["avg_price"],
                "exit_price":  price,
                "qty":         close_qty,
                "pnl":         pnl - fee,
                "entry_time":  pos.get("entry_time", now),
                "exit_time":   now,
                "sl":          pos.get("sl"),
                "tp":          pos.get("tp"),
                "exit_reason": reason,
            })

            extra = qty - close_qty
            if abs(new_amt) < 1e-9:
                self.positions.pop(symbol, None)
            elif extra > 1e-9:
                new_margin = (extra * price) / leverage
                if self.balance < new_margin:
                    raise ValueError(f"餘額不足進行反向開倉")
                self.balance -= new_margin
                self.positions[symbol] = {"amt": new_amt, "avg_price": price, "margin": new_margin,
                                           "leverage": leverage, "sl": sl, "tp": tp, "entry_time": now}
                self._log_entry(symbol, side, extra, price, leverage, sl, tp, source, now)
            else:
                self.positions[symbol] = {
                    "amt": new_amt, "avg_price": pos["avg_price"], "margin": pos["margin"] * (1 - ratio),
                    "leverage": pos["leverage"],
                    "sl": sl if sl is not None else pos.get("sl"),
                    "tp": tp if tp is not None else pos.get("tp"),
                    "entry_time": pos.get("entry_time", now),
                }

    def due_funding_buckets(self) -> list[int]:
        """回傳自上次結算後、已跨過的資金費結算 bucket（通常只有 1 個）。"""
        cur_bucket = int(time.time()) // FUNDING_INTERVAL_SEC
        buckets = list(range(self.last_funding_bucket + 1, cur_bucket + 1))
        self.last_funding_bucket = cur_bucket
        return buckets

    def apply_funding(self, symbol: str, rate: float, mark_price: float) -> float:
        """
        結算一次資金費：正費率多方付空方收，負費率反之。
        金額 = 持倉名義價值(標記價 * 數量) * 費率，方向依持倉正負決定。
        回傳實際入帳金額（負值代表支出）。
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return 0.0
        notional = abs(pos["amt"]) * mark_price
        payment = notional * rate if pos["amt"] > 0 else -notional * rate
        self.balance -= payment
        self.total_fees += payment
        return -payment

    def _liquidation_price(self, amt: float, entry: float, leverage: int) -> float:
        mmr = MAINTENANCE_MARGIN_RATE
        if amt > 0:
            return entry * (leverage - 1) / leverage / (1 - mmr)
        return entry * (leverage + 1) / leverage / (1 + mmr)

    def check_liquidation(self, symbol: str, price: float) -> bool:
        """
        簡化強平判斷：權益(保證金+未實現盈虧) <= 維持保證金 時強制平倉。
        強平時保證金全損（不歸還餘額），符合逐倉合約「最多虧光保證金」的簡化模型。
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        amt, entry, margin = pos["amt"], pos["avg_price"], pos["margin"]
        notional = abs(amt) * price
        pnl = amt * (price - entry)
        maintenance = notional * MAINTENANCE_MARGIN_RATE
        if margin + pnl <= maintenance:
            self.trade_history.append({
                "symbol":      symbol,
                "side":        "BUY" if amt > 0 else "SELL",
                "entry_price": entry,
                "exit_price":  price,
                "qty":         abs(amt),
                "pnl":         -margin,   # 保證金全損
                "entry_time":  pos.get("entry_time", int(time.time())),
                "exit_time":   int(time.time()),
                "sl":          pos.get("sl"),
                "tp":          pos.get("tp"),
                "exit_reason": "LIQUIDATION",
            })
            self.positions.pop(symbol, None)
            # 強平後同幣對的掛單一併取消，避免舊掛單之後觸價、
            # 在使用者不知情的情況下用新保證金開出一筆全新倉位
            self.orders = [o for o in self.orders if o["symbol"] != symbol]
            return True
        return False

    def check_sl_tp(self, symbol: str, price: float) -> bool:
        """檢查持倉是否觸及止損/止盈，觸發則以市價（taker fee）全部平倉。回傳是否已平倉。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        sl, tp = pos.get("sl"), pos.get("tp")
        if sl is None and tp is None:
            return False
        is_long = pos["amt"] > 0
        hit_sl = sl is not None and ((is_long and price <= sl) or (not is_long and price >= sl))
        hit_tp = tp is not None and ((is_long and price >= tp) or (not is_long and price <= tp))
        if hit_sl or hit_tp:
            close_side = "SELL" if is_long else "BUY"
            self._fill(symbol, close_side, abs(pos["amt"]), price, pos["leverage"], TAKER_FEE,
                       reason="SL" if hit_sl else "TP")
            return True
        return False

    def set_sl_tp(self, symbol: str, sl: float | None, tp: float | None) -> dict:
        pos = self.positions.get(symbol)
        if pos is None:
            raise ValueError(f"沒有 {symbol} 的持倉")
        pos["sl"], pos["tp"] = sl, tp
        return pos

    def get_trade_history(self, symbol: str | None = None, limit: int = 100) -> list:
        hist = self.trade_history if symbol is None else [t for t in self.trade_history if t["symbol"] == symbol]
        return hist[-limit:]

    def get_entry_log(self, symbol: str | None = None, limit: int = 100) -> list:
        log = self.entry_log if symbol is None else [e for e in self.entry_log if e["symbol"] == symbol]
        return log[-limit:]

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
                "sl": pos.get("sl"),
                "tp": pos.get("tp"),
                "entryTime": pos.get("entry_time"),
                "liqPrice": self._liquidation_price(amt, pos["avg_price"], pos["leverage"]),
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
            "totalFees": self.total_fees,
        }
