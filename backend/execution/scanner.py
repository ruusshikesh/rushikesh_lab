"""
Rush Algo — Live Scanner & Execution Engine

This is the piece that actually RUNS deployed strategies. Without it,
"Deploy" and "Forward Test" only register a strategy — nothing scans
the market or places trades.

Runs every SCAN_INTERVAL_SEC seconds during market hours (9:15-15:30 IST,
Mon-Fri). For each LIVE deployment:
  1. Evaluate entry conditions on the primary timeframe
  2. If BUY signal + MTF confirmation passes + compliance allows it -> place order
  3. Check open positions for TARGET1 (partial book + trailing SL activate),
     TARGET2 (full exit), or STOP_LOSS/TRAILING_SL (full exit)
  4. No re-entry on the same symbol same day after a close
  5. Daily reset of P&L and re-entry blocklist at 09:15 IST
"""
from __future__ import annotations
import logging
import math
from datetime import datetime
from typing import Callable, Dict, Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from models.schemas import Broker, Deployment, Order, OrderStatus, TradeType
from data.fetcher import fetch_ohlcv, get_live_quote
from indicators.library import compute_all
from strategy.engine import evaluate_signal, check_mtf_confirmation
from execution.risk_manager import risk_manager
from compliance.engine import ComplianceEngine, Event
from alerts.telegram_bot import telegram
from brokers.paper_broker import PaperBroker

logger = logging.getLogger(__name__)
IST    = pytz.timezone(settings.TZ)

MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (h, m) >= MARKET_OPEN and (h, m) <= MARKET_CLOSE


def _make_broker(broker: Broker, paper: PaperBroker):
    if broker == Broker.paper:
        return paper
    if broker == Broker.fyers:
        from brokers.fyers_client import FyersClient
        return FyersClient()
    if broker == Broker.zerodha:
        from brokers.zerodha_client import ZerodhaClient
        return ZerodhaClient()
    if broker == Broker.dhan:
        from brokers.dhan_client import DhanClient
        return DhanClient()
    raise ValueError(f"Unknown broker: {broker}")


class LiveScanner:
    """
    deployments: the SAME dict object main.py holds (mutated in place,
    so main.py's API responses always reflect the latest scanner state).
    """
    def __init__(self, deployments: Dict[str, Deployment],
                 compliance: ComplianceEngine,
                 ws_broadcast: Optional[Callable] = None,
                 save_deployments_fn: Optional[Callable] = None):
        self.deployments = deployments
        self.compliance  = compliance
        self.ws_broadcast = ws_broadcast
        self.save_deployments_fn = save_deployments_fn
        self.scheduler   = AsyncIOScheduler(timezone=IST)
        self._brokers: Dict[str, object] = {}
        self._paper = PaperBroker(settings.TOTAL_CAPITAL)

    def _broker_for(self, dep: Deployment):
        if dep.id not in self._brokers:
            self._brokers[dep.id] = _make_broker(dep.broker, self._paper)
        return self._brokers[dep.id]

    def start(self):
        self.scheduler.add_job(
            self._scan_all, IntervalTrigger(seconds=settings.SCAN_INTERVAL_SEC),
            id="main_scan", replace_existing=True,
        )
        self.scheduler.add_job(
            self._daily_reset,
            CronTrigger(hour=9, minute=15, timezone=IST, day_of_week="mon-fri"),
            id="daily_reset", replace_existing=True,
        )
        self.scheduler.start()
        logger.info("LiveScanner started (every %ds during market hours)", settings.SCAN_INTERVAL_SEC)

    def stop(self):
        self.scheduler.shutdown(wait=False)

    async def _daily_reset(self):
        for dep in self.deployments.values():
            dep.today_pnl = 0.0
            dep.blocked_symbols = []
        # FIX (bug D): the scheduled daily reset must also clear the daily-loss kill
        # switches — otherwise yesterday's loss-triggered halt persists into today
        # and the scanner stays blocked (the kill_active check at the top of
        # _scan_all returns early forever). Only auto-clear a kill that was caused
        # by the daily loss limit; a manual kill stays until manually reset.
        if risk_manager.kill_active and "Daily loss" in risk_manager.kill_reason:
            risk_manager.reset_kill()
            logger.info("Daily reset: cleared risk-manager daily-loss kill switch")
        if self.compliance.kill_active and "Daily loss" in (self.compliance.kill_reason or ""):
            self.compliance.reset_kill()
            logger.info("Daily reset: cleared compliance daily-loss kill switch")
        # Reset the risk manager's daily P&L via its own API (also refreshes date).
        risk_manager._daily_pnl = 0.0
        risk_manager._last_date = datetime.now(IST).date()
        self.compliance.log(Event.COMPLIANCE_WARN, event="daily_reset")
        if self.ws_broadcast:
            await self.ws_broadcast({"type": "daily_reset", "timestamp": datetime.now(IST).isoformat()})

    async def _scan_all(self):
        if not _is_market_open():
            return
        if self.compliance.kill_active or risk_manager.kill_active:
            return
        for dep in list(self.deployments.values()):
            if dep.status != "LIVE":
                continue
            try:
                await self._process(dep)
            except Exception as exc:
                logger.error("[%s] scan error: %s", dep.id, exc)

    async def _process(self, dep: Deployment):
        strategy = dep.strategy
        broker   = self._broker_for(dep)
        symbol   = strategy.symbol

        df     = fetch_ohlcv(symbol, timeframe=str(strategy.primary_tf.value), days=60)
        df_ind = compute_all(df)
        result = evaluate_signal(strategy, df_ind)

        if self.ws_broadcast:
            await self.ws_broadcast({"type": "signal", "dep_id": dep.id, "symbol": symbol, **result})

        # ── ENTRY ────────────────────────────────────────────────────────────
        already_in = any(o.symbol == symbol for o in dep.open_orders)
        now_time   = datetime.now(IST).strftime("%H:%M")
        is_intra   = (strategy.trade_type == TradeType.intraday)
        # FIX: ENTRY_END was defined in config (and .env.example) but never enforced,
        # so for intraday the scanner kept opening fresh positions right up to the
        # EOD exit window. Gate new entries to the [ENTRY_START, ENTRY_END] window.
        time_ok    = (not is_intra) or (settings.ENTRY_START <= now_time <= settings.ENTRY_END)

        if (result["signal"] == "BUY"
                and result["confidence"] >= 60
                and not already_in
                and symbol not in dep.blocked_symbols
                and time_ok
                and len(dep.open_orders) < strategy.risk.max_positions):

            if strategy.mtf.enabled:
                mtf_fetch = lambda s, tf: fetch_ohlcv(s, timeframe=tf, days=60)
                confirmed, reason = check_mtf_confirmation(strategy, mtf_fetch)
                if not confirmed:
                    self.compliance.log(Event.MTF_REJECTED, dep_id=dep.id, symbol=symbol, reason=reason)
                    return
                self.compliance.log(Event.MTF_CONFIRMED, dep_id=dep.id, symbol=symbol, reason=reason)

            allowed, reason = self.compliance.can_place_order()
            if not allowed:
                self.compliance.log(Event.RATE_LIMITED, dep_id=dep.id, symbol=symbol, reason=reason)
                return

            quote = get_live_quote(symbol)
            price = quote["ltp"]
            if not price or price <= 0:
                logger.warning("[%s] live price is 0 for %s — skipping entry", dep.id, symbol)
                return

            trade_amt = min(strategy.risk.trade_amount, settings.TOTAL_CAPITAL * settings.MAX_TRADE_PCT / 100)
            qty = max(1, math.floor(trade_amt / price))
            sl  = round(price * (1 - strategy.risk.sl_pct / 100), 2)
            t1  = round(price * (1 + strategy.risk.target1_pct / 100), 2)
            t2  = round(price * (1 + strategy.risk.target2_pct / 100), 2)
            algo_tag = strategy.algo_id or ComplianceEngine.make_algo_tag(strategy.name, dep.id)

            order = Order(
                symbol=symbol, side="BUY", qty=qty, order_type="LIMIT",
                price=round(price, 2), stop_loss=sl, target1=t1, target2=t2,
                broker=dep.broker, strategy_id=strategy.id or "", strategy_name=strategy.name,
                algo_id=algo_tag, trade_type=strategy.trade_type,
            )

            # ── APPROVE-FIRST MODE ──────────────────────────────────────────
            # When APPROVE_FIRST is on, we do NOT place the order here. Instead
            # we send a Telegram alert with Approve/Skip buttons and stop. The
            # order is placed only if you tap the button, via the callback wired
            # in main.py (_place_from_signal), which routes through the correct
            # broker, honours paper mode, preserves trade_type, and registers
            # the order in dep.open_orders so exits are still managed.
            #
            # dep_id is included so the callback knows WHICH deployment (and
            # therefore which broker / paper flag) this signal belongs to -
            # without it the callback cannot safely determine where to route
            # the order, and refuses rather than guessing.
            if getattr(settings, "APPROVE_FIRST", False):
                telegram.send_signal_alert({
                    "dep_id":        dep.id,
                    "symbol":        symbol,
                    "side":          "BUY",
                    "qty":           qty,
                    "price":         round(price, 2),
                    "stop_loss":     sl,
                    "target1":       t1,
                    "target2":       t2,
                    "order_type":    "LIMIT",
                    "strategy_id":   strategy.id or "",
                    "strategy_name": strategy.name,
                    "algo_id":       algo_tag,
                    "trade_type":    strategy.trade_type,
                    "score":         result.get("confidence"),
                    "reason":        " · ".join(result.get("reasons", [])[:3]),
                })
                self.compliance.log(Event.SIGNAL, dep_id=dep.id, symbol=symbol,
                                    qty=qty, price=round(price, 2), algo_tag=algo_tag,
                                    reason="approve-first: awaiting Telegram approval")
                logger.info("[%s] %s signal sent for approval (approve-first mode)", dep.id, symbol)
                return

            placed = broker.place_order(order)

            if placed.status == OrderStatus.OPEN:
                dep.open_orders.append(placed)
                dep.trade_count += 1
                self.compliance.record_order()
                self.compliance.log(Event.ORDER_PLACED, dep_id=dep.id, symbol=symbol,
                                    qty=qty, price=round(price, 2), algo_tag=algo_tag)
                telegram.order_alert(symbol, "BUY", qty, price, sl, t1, t2, dep.broker.value)
                if self.ws_broadcast:
                    await self.ws_broadcast({"type": "order", "order": placed.model_dump(mode="json")})
                if self.save_deployments_fn:
                    self.save_deployments_fn()
            else:
                self.compliance.log(Event.ORDER_REJECTED, dep_id=dep.id, symbol=symbol, algo_tag=algo_tag)

        # ── EXIT / PARTIAL BOOKING CHECK ────────────────────────────────────
        for order in list(dep.open_orders):
            if order.status != OrderStatus.OPEN:
                continue
            try:
                await self._check_exit(dep, broker, order)
            except Exception as exc:
                logger.warning("[%s] exit check error for %s: %s", dep.id, order.symbol, exc)

    async def _check_exit(self, dep: Deployment, broker, order: Order):
        quote = get_live_quote(order.symbol)
        ltp   = quote["ltp"]
        if not ltp or ltp <= 0:
            return

        # Partial booking at TARGET1 — sell half, let the rest run with a
        # trailing stop. This is a real SELL order for half the quantity,
        # not a special broker capability — any broker that supports
        # place_order(side="SELL") already supports this.
        if not order.partial_booked and ltp >= order.target1:
            half_qty = max(1, order.qty // 2)
            # FIX (bug 4): for the paper broker, reduce the existing long via
            # reduce_position() — NOT place_order(SELL), which mismodeled it as a
            # short. Real brokers correctly reduce a real holding with a SELL order,
            # so keep that path for them.
            booked = False
            partial_pnl = 0.0
            if hasattr(broker, "reduce_position"):
                res = broker.reduce_position(order.id, half_qty, ltp)
                if res:
                    partial_pnl = res["pnl"]
                    order.qty  -= half_qty
                    booked = True
            else:
                sell_order = Order(
                    symbol=order.symbol, side="SELL", qty=half_qty, order_type="MARKET",
                    price=ltp, stop_loss=order.stop_loss, target1=order.target1, target2=order.target2,
                    broker=order.broker, strategy_id=order.strategy_id, strategy_name=order.strategy_name,
                    algo_id=order.algo_id, trade_type=order.trade_type,
                )
                sell_result = broker.place_order(sell_order)
                if sell_result.status == OrderStatus.OPEN:
                    partial_pnl = (ltp - order.price) * half_qty
                    order.qty -= half_qty
                    booked = True

            if booked:
                order.partial_booked = True
                # Activate trailing stop above original SL once partial is booked
                trail = round(ltp * (1 - dep.strategy.risk.trailing_sl_pct / 100), 2)
                if trail > order.stop_loss:
                    order.stop_loss = trail
                dep.today_pnl += partial_pnl
                dep.total_pnl += partial_pnl
                risk_manager.record_pnl(partial_pnl)
                self.compliance.log(Event.PARTIAL_BOOK, dep_id=dep.id, symbol=order.symbol,
                                    qty=half_qty, price=ltp, pnl=round(partial_pnl, 2))
                telegram.trade_closed_alert(order.symbol, partial_pnl, ltp, "PARTIAL_BOOK (target1)")
                if self.ws_broadcast:
                    await self.ws_broadcast({"type": "partial_book", "order": order.model_dump(mode="json")})
                if self.save_deployments_fn:
                    self.save_deployments_fn()
            # If the partial sell failed, partial_booked stays False and it will be
            # retried next scan — acceptable, but logged so repeated failures surface.
            elif not booked:
                logger.warning("[%s] partial book failed for %s — will retry", dep.id, order.symbol)

        # Update trailing stop continuously once partial booking is active
        if order.partial_booked:
            trail = round(ltp * (1 - dep.strategy.risk.trailing_sl_pct / 100), 2)
            if trail > order.stop_loss:
                order.stop_loss = trail

        exit_price = None
        reason     = None
        now_time   = datetime.now(IST).strftime("%H:%M")
        today      = datetime.now(IST).date()

        # 1) Hard target / stop (both intraday and positional)
        if ltp >= order.target2:
            exit_price, reason = order.target2, "TARGET2"
        elif ltp <= order.stop_loss:
            exit_price, reason = order.stop_loss, ("TRAILING_SL" if order.partial_booked else "STOP_LOSS")

        # 2) Intraday EOD exit — only losing positions forced out at INTRADAY_EXIT,
        # everything still open is forced out by INTRADAY_LAST_EXIT
        if exit_price is None and order.trade_type == TradeType.intraday:
            if now_time >= settings.INTRADAY_EXIT:
                pnl_so_far = (ltp - order.price) * order.qty
                if pnl_so_far < 0:
                    exit_price, reason = ltp, "EOD_LOSS"
                elif now_time >= settings.INTRADAY_LAST_EXIT:
                    exit_price, reason = ltp, "EOD_FINAL"

        # 3) HYBRID positional management — carry a runner 2-3 days, but don't
        # carry dead risk forever. Requires MAX_HOLD_DAYS / SCRATCH_EOD_ENABLED
        # in config.py (defaults used here if not set, via getattr).
        if exit_price is None and order.trade_type == TradeType.positional and order.entry_time:
            # TIMEZONE FIX: `today` is IST-aware, but brokers stamp entry_time with
            # a NAIVE datetime.now() (machine-local). Comparing the two directly
            # only works while the machine clock happens to BE IST. On a UTC cloud
            # VPS - which a static-IP requirement for live Zerodha orders pushes
            # you toward - every day after 18:30 UTC is already the next date in
            # IST, so days_held inflates by 1 and a MAX_HOLD_DAYS=3 position gets
            # force-exited after only 2 days. Normalise naive stamps to IST first.
            _entry = order.entry_time
            if _entry.tzinfo is None:
                _entry = _entry.astimezone(IST) if hasattr(_entry, "astimezone") else _entry
                try:
                    _entry = IST.localize(order.entry_time)
                except (AttributeError, ValueError):
                    pass
            else:
                _entry = _entry.astimezone(IST)

            entry_date     = _entry.date()
            entry_time_str = _entry.strftime("%H:%M")
            days_held      = (today - entry_date).days
            max_hold_days  = getattr(settings, "MAX_HOLD_DAYS", 3)
            scratch_on     = getattr(settings, "SCRATCH_EOD_ENABLED", True)

            # 3a) Carry cap: exit at market once held >= MAX_HOLD_DAYS
            if days_held >= max_hold_days:
                exit_price, reason = ltp, "MAX_HOLD"

            # 3b) Same-day scratch: entered earlier today, near close, still red,
            # nothing booked. Green-but-not-target positions are CARRIED (the
            # hybrid intent) — only red duds with no progress get cut.
            elif (scratch_on
                  and days_held == 0
                  and entry_time_str < settings.INTRADAY_EXIT
                  and now_time >= settings.INTRADAY_EXIT
                  and not order.partial_booked
                  and ltp < order.price):
                exit_price, reason = ltp, "SCRATCH_EOD"

        if exit_price is not None and hasattr(broker, "close_order"):
            closed = broker.close_order(order.id, exit_price, reason)
            if closed:
                dep.today_pnl += closed.pnl or 0
                dep.total_pnl += closed.pnl or 0
                dep.open_orders.remove(order)
                dep.closed_trades.append(closed)
                dep.blocked_symbols.append(order.symbol)  # no re-entry same symbol today
                risk_manager.record_pnl(closed.pnl or 0)
                self.compliance.log(Event.ORDER_CLOSED, dep_id=dep.id, symbol=order.symbol,
                                    reason=reason, pnl=round(closed.pnl or 0, 2))
                telegram.trade_closed_alert(order.symbol, closed.pnl or 0, exit_price, reason)
                if self.ws_broadcast:
                    await self.ws_broadcast({"type": "close", "order": closed.model_dump(mode="json")})
                if self.save_deployments_fn:
                    self.save_deployments_fn()
            else:
                # Exit was attempted but NOT confirmed by the broker. The position
                # is intentionally left in open_orders so it's retried next scan.
                # Surface this loudly — an unclosed live position needs attention.
                order.exit_retry_count = (order.exit_retry_count or 0) + 1
                logger.error("[%s] EXIT NOT CONFIRMED for %s (reason=%s, attempt #%d) — still OPEN",
                             dep.id, order.symbol, reason, order.exit_retry_count)
                self.compliance.log(Event.ORDER_REJECTED, dep_id=dep.id, symbol=order.symbol,
                                    reason=f"EXIT_FAILED:{reason}", attempt=order.exit_retry_count)
                telegram.exit_failed_alert(order.symbol, reason, order.exit_retry_count)
                if self.ws_broadcast:
                    await self.ws_broadcast({"type": "exit_failed", "dep_id": dep.id,
                                             "symbol": order.symbol, "reason": reason,
                                             "attempt": order.exit_retry_count})
