"""
Rush Algo — Risk Manager
Handles position sizing, SL/target calculation, partial booking,
daily loss tracking, and kill switch.
"""
from __future__ import annotations
import logging
from datetime import date
from typing import Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self._daily_pnl:   float = 0.0
        self._last_date:   date  = date.today()
        self._kill_active: bool  = False
        self._kill_reason: str   = ""

    # ── Daily P&L tracking ────────────────────────────────────────────────────

    def _refresh_day(self):
        if date.today() != self._last_date:
            self._daily_pnl  = 0.0
            self._last_date  = date.today()
            # FIX (bug A): a DAILY-loss kill switch must clear when the day rolls
            # over — otherwise a single bad day permanently halts trading until a
            # manual reset. Only auto-clear kills that were triggered by the daily
            # loss limit; a manually-set kill stays until manually cleared.
            if self._kill_active and "Daily loss" in self._kill_reason:
                self._kill_active = False
                self._kill_reason = ""
                logger.info("Risk: new day — daily-loss kill switch auto-reset")
            logger.info("Risk: daily P&L reset")

    def record_pnl(self, pnl: float):
        self._refresh_day()
        self._daily_pnl += pnl
        kill_threshold = -(settings.TOTAL_CAPITAL * settings.KILL_SWITCH_PCT / 100)
        if self._daily_pnl <= kill_threshold and not self._kill_active:
            self.activate_kill(
                f"Daily loss ₹{abs(self._daily_pnl):,.0f} exceeded "
                f"{settings.KILL_SWITCH_PCT}% threshold"
            )

    @property
    def daily_pnl(self) -> float:
        self._refresh_day()
        return self._daily_pnl

    # ── Kill switch ───────────────────────────────────────────────────────────

    @property
    def kill_active(self) -> bool:
        return self._kill_active

    def activate_kill(self, reason: str = "Manual"):
        self._kill_active = True
        self._kill_reason = reason
        logger.critical("⚠️  RISK KILL SWITCH: %s", reason)

    def reset_kill(self):
        self._kill_active = False
        self._kill_reason = ""
        logger.info("Kill switch reset")

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    # ── Position sizing ───────────────────────────────────────────────────────

    def calc_qty(self, price: float, trade_amount: Optional[float] = None) -> int:
        """
        Returns qty to buy.
        Uses the LOWER of:
          - fixed trade amount (₹30,000 default)
          - 5% of total capital
        Minimum 1 share.
        """
        if price <= 0:
            return 0
        fixed_amount  = trade_amount or settings.MAX_TRADE_AMOUNT
        pct_amount    = settings.TOTAL_CAPITAL * settings.MAX_TRADE_PCT / 100
        use_amount    = min(fixed_amount, pct_amount)
        qty           = int(use_amount // price)
        return max(1, qty)

    # ── SL / Target calculation ───────────────────────────────────────────────

    def calc_levels(self, entry: float, sl_pct: float = None,
                    t1_pct: float = None, t2_pct: float = None) -> Tuple[float, float, float]:
        """Returns (stop_loss, target1, target2)."""
        sl  = round(entry * (1 - (sl_pct or settings.DEFAULT_SL_PCT) / 100), 2)
        t1  = round(entry * (1 + (t1_pct or settings.DEFAULT_TARGET1_PCT) / 100), 2)
        t2  = round(entry * (1 + (t2_pct or settings.DEFAULT_TARGET2_PCT) / 100), 2)
        return sl, t1, t2

    def calc_trailing_sl(self, current_price: float, trail_pct: float = None) -> float:
        """Returns trailing stop loss price."""
        pct = trail_pct or settings.DEFAULT_TRAIL_PCT
        return round(current_price * (1 - pct / 100), 2)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        self._refresh_day()
        kill_level  = -(settings.TOTAL_CAPITAL * settings.KILL_SWITCH_PCT / 100)  # negative, e.g. -200000
        # FIX: old formula `kill_level - daily_pnl` was virtually always negative
        # (e.g. -200000 - 0 = -200000), so the frontend's `remaining_loss > 0`
        # color check was ALWAYS false — the "Daily Remaining" stat showed red
        # even on a flat/profitable day with zero risk. Recompute as a clean,
        # always-non-negative rupee buffer remaining before the kill switch fires.
        loss_so_far       = max(0.0, -self._daily_pnl)              # 0 if flat/profit
        remaining_buffer  = max(0.0, abs(kill_level) - loss_so_far)  # >=0 always
        return {
            "kill_active":    self._kill_active,
            "kill_reason":    self._kill_reason,
            "daily_pnl":      round(self._daily_pnl, 2),
            "kill_threshold": round(kill_level, 2),
            "pnl_pct":        round(self._daily_pnl / settings.TOTAL_CAPITAL * 100, 2),
            "remaining_loss": round(remaining_buffer, 2),
        }


# Shared singleton
risk_manager = RiskManager()
