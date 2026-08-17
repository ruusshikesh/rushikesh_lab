"""
StrykeX — Paper Trading Broker (in-memory simulation)
FIX: close_order cash logic now correctly handles both BUY and SELL positions.
     Previously, SELL close always added 0 to cash.
"""
from __future__ import annotations
import threading
import uuid
from datetime import datetime
from typing import List, Optional

from models.schemas import Order, OrderStatus


class PaperBroker:
    def __init__(self, initial_capital: float = 100_000.0):
        self.capital = initial_capital
        self.cash    = initial_capital
        self._orders: dict = {}
        self._lock   = threading.Lock()

    def place_order(self, order: Order) -> Order:
        with self._lock:
            order.id              = str(uuid.uuid4())[:8]
            order.broker_order_id = f"PAPER-{order.id}"
            order.entry_time      = datetime.now()
            cost = order.price * order.qty
            if order.side == "BUY":
                if cost > self.cash:
                    order.status = OrderStatus.REJECTED
                    return order
                self.cash -= cost          # deduct cost for BUY entry
            else:
                # Short SELL: receive proceeds upfront (simplified)
                self.cash += cost
            order.status = OrderStatus.OPEN
            self._orders[order.id] = order
            return order

    def close_order(self, order_id: str, exit_price: float, reason: str = "MANUAL") -> Optional[Order]:
        with self._lock:
            order = self._orders.get(order_id)
            if not order or order.status != OrderStatus.OPEN:
                return None

            order.exit_price  = exit_price
            order.exit_time   = datetime.now()
            order.exit_reason = reason

            if order.side == "BUY":
                # FIX: BUY close — receive exit proceeds, profit = (exit - entry) * qty
                order.pnl  = (exit_price - order.price) * order.qty
                self.cash += exit_price * order.qty     # return sale proceeds
            else:
                # FIX: SELL (short) close — buy back at exit price
                # Proceeds were received at entry; now pay exit cost
                order.pnl  = (order.price - exit_price) * order.qty
                self.cash -= exit_price * order.qty     # pay to close short

            order.status = OrderStatus.CLOSED
            return order

    def reduce_position(self, order_id: str, qty: int, exit_price: float) -> Optional[dict]:
        """
        FIX (bug 4): correctly book a PARTIAL exit of an existing long — sell `qty`
        shares of an open BUY at exit_price. Previously the scanner did this via
        place_order(side="SELL"), which the broker treated as OPENING A SHORT
        (adding spurious proceeds and leaving a phantom untracked order). This
        instead reduces the existing position's qty, returns the proceeds to cash,
        and reports the realized P&L on the sold portion.
        """
        with self._lock:
            order = self._orders.get(order_id)
            if not order or order.status != OrderStatus.OPEN or order.side != "BUY":
                return None
            qty = min(qty, order.qty)
            if qty <= 0:
                return None
            realized = (exit_price - order.price) * qty
            self.cash += exit_price * qty      # proceeds from selling part of the long
            order.qty -= qty                   # reduce the remaining position
            return {"qty": qty, "exit_price": exit_price, "pnl": round(realized, 2)}

    def get_open_orders(self) -> List[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.status == OrderStatus.OPEN]

    def get_all_orders(self) -> List[Order]:
        with self._lock:
            return list(self._orders.values())

    def get_pnl(self) -> dict:
        with self._lock:
            closed    = [o for o in self._orders.values() if o.status == OrderStatus.CLOSED]
            total_pnl = sum(o.pnl or 0 for o in closed)
            today_pnl = sum(
                o.pnl or 0 for o in closed
                if o.exit_time and o.exit_time.date() == datetime.now().date()
            )
            return {
                "total_pnl":    round(total_pnl, 2),
                "today_pnl":    round(today_pnl, 2),
                "cash":         round(self.cash,  2),
                "total_trades": len(closed),
            }
