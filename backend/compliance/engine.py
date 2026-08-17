"""
Rush Algo — Compliance Engine
SEBI self-managed algo: stay under 10 orders/second (no registration needed).
Kill switch, audit trail, rate limiter.
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz

from config import settings

logger = logging.getLogger(__name__)
IST    = pytz.timezone(settings.TZ)
LOG_DIR= "logs"


class Event:
    SIGNAL          = "SIGNAL"
    ORDER_PLACED    = "ORDER_PLACED"
    ORDER_REJECTED  = "ORDER_REJECTED"
    ORDER_CLOSED    = "ORDER_CLOSED"
    PARTIAL_BOOK    = "PARTIAL_BOOK"
    KILL_ACTIVATED  = "KILL_ACTIVATED"
    KILL_RESET      = "KILL_RESET"
    RATE_LIMITED    = "RATE_LIMITED"
    DAILY_LIMIT_HIT = "DAILY_LIMIT_HIT"
    MTF_CONFIRMED   = "MTF_CONFIRMED"
    MTF_REJECTED    = "MTF_REJECTED"
    COMPLIANCE_WARN = "COMPLIANCE_WARN"


class ComplianceEngine:
    def __init__(self):
        self._lock              = threading.Lock()
        self._kill_active       = False
        self._kill_time:  Optional[datetime] = None
        self._kill_reason = ""
        self._order_times: deque = deque()
        self._daily_count = 0
        self._daily_date  = date.today()
        self._audit: List[Dict] = []
        os.makedirs(LOG_DIR, exist_ok=True)
        logger.info("ComplianceEngine ready (%d orders/sec max, %d/day)",
                    settings.MAX_ORDERS_PER_SEC, settings.MAX_ORDERS_PER_DAY)

    # ── Kill switch ───────────────────────────────────────────────────────────
    @property
    def kill_active(self) -> bool: return self._kill_active

    def activate_kill(self, reason: str = "Manual") -> None:
        with self._lock:
            self._kill_active = True
            self._kill_time   = datetime.now(IST)
            self._kill_reason = reason
        self._write(Event.KILL_ACTIVATED, reason=reason)
        logger.critical("⚠️  KILL SWITCH: %s", reason)

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def reset_kill(self) -> None:
        with self._lock:
            self._kill_active = False
            self._kill_time   = None
            self._kill_reason = ""
        self._write(Event.KILL_RESET)
        logger.info("Kill switch reset")

    # ── Rate limiting ─────────────────────────────────────────────────────────
    def _refresh_daily(self):
        today = date.today()
        if today != self._daily_date:
            self._daily_count = 0
            self._daily_date  = today

    def can_place_order(self) -> Tuple[bool, str]:
        with self._lock:
            if self._kill_active:
                return False, f"Kill switch active: {self._kill_reason}"
            self._refresh_daily()
            if self._daily_count >= settings.MAX_ORDERS_PER_DAY:
                return False, f"Daily limit {settings.MAX_ORDERS_PER_DAY} reached"
            now = time.monotonic()
            while self._order_times and self._order_times[0] < now - 1.0:
                self._order_times.popleft()
            if len(self._order_times) >= settings.MAX_ORDERS_PER_SEC:
                return False, f"Rate limit {settings.MAX_ORDERS_PER_SEC}/sec exceeded"
            return True, "ok"

    def record_order(self) -> None:
        with self._lock:
            self._refresh_daily()
            self._order_times.append(time.monotonic())
            self._daily_count += 1

    # ── Audit log ─────────────────────────────────────────────────────────────
    def log(self, event_type: str, dep_id: str = "",
            symbol: str = "", **kwargs: Any) -> None:
        entry = {"ts": datetime.now(IST).isoformat(), "event": event_type,
                 "dep_id": dep_id, "symbol": symbol, "details": kwargs}
        self._write_entry(entry)

    def _write(self, event_type: str, **kw: Any) -> None:
        self._write_entry({"ts": datetime.now(IST).isoformat(),
                           "event": event_type, "details": kw})

    def _write_entry(self, entry: dict) -> None:
        with self._lock:
            self._audit.append(entry)
            if len(self._audit) > 10_000:
                self._audit.pop(0)
        try:
            fname = os.path.join(LOG_DIR, f"audit_{date.today().strftime('%Y%m%d')}.jsonl")
            with open(fname, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.warning("Audit write failed: %s", exc)

    def get_audit(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return list(self._audit[-limit:])

    def status(self) -> dict:
        with self._lock:
            self._refresh_daily()
            now = time.monotonic()
            opm = sum(1 for t in self._order_times if t >= now - 1.0)
            return {
                "kill_active":     self._kill_active,
                "kill_time":       self._kill_time.isoformat() if self._kill_time else None,
                "kill_reason":     self._kill_reason,
                "orders_last_sec": opm,
                "rate_limit":      settings.MAX_ORDERS_PER_SEC,
                "daily_orders":    self._daily_count,
                "daily_limit":     settings.MAX_ORDERS_PER_DAY,
                "daily_remaining": max(0, settings.MAX_ORDERS_PER_DAY - self._daily_count),
            }

    @staticmethod
    def make_algo_tag(strategy_name: str, dep_id: str) -> str:
        return f"RA-{dep_id}"[:20]   # RA = Rush Algo
