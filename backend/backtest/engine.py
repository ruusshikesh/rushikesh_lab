"""
Rush Algo — Backtest Engine
Features: partial booking at T1, trailing SL, EOD exit for intraday,
positional hold, entry time filter, re-entry blocked same day.
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd

from config import settings
from models.schemas import BacktestRequest, BacktestResult, TradeRecord, TradeType
from indicators.library import compute_all
from strategy.engine import evaluate_signal

logger  = logging.getLogger(__name__)
WARMUP  = 200


def _fill_prices(ideal: float, slippage_pct: float):
    """Return (buy_fill, sell_fill) — buys fill higher, sells lower, by slippage."""
    return (round(ideal * (1 + slippage_pct / 100), 4),
            round(ideal * (1 - slippage_pct / 100), 4))

_ANN = {
    "1min":  252 * 375,
    "3min":  252 * 125,
    "5min":  252 * 75,
    "15min": 252 * 26,
    "30min": 252 * 13,
    "1hr":   252 * 6,
    "1day":  252,
}


@dataclass
class Position:
    entry_date:      str
    entry_price:     float
    qty:             int
    stop_loss:       float
    target1:         float
    target2:         float
    trail_sl:        float
    partial_booked:  bool  = False
    partial_qty:     int   = 0
    partial_fill:    float = 0.0   # actual fill price of the partial booking


def run_backtest(req: BacktestRequest, df: pd.DataFrame) -> BacktestResult:
    if len(df) <= WARMUP:
        raise ValueError(
            f"Not enough data: {len(df)} bars, need >{WARMUP}. "
            "Use a longer date range or a higher timeframe."
        )
    # FIX: initial_capital=0 caused an unhandled ZeroDivisionError in the
    # total_return calculation below, surfacing as a raw 500 to the user
    # instead of a clear message. Negative capital is also nonsensical.
    if req.initial_capital <= 0:
        raise ValueError(
            f"initial_capital must be greater than 0 (got {req.initial_capital})."
        )

    strategy = req.strategy
    risk     = strategy.risk
    capital  = req.initial_capital
    tf_val   = str(strategy.primary_tf.value)
    is_intra = (strategy.trade_type == TradeType.intraday)

    df_ind = compute_all(df)
    cash   = capital
    pos:  Optional[Position] = None
    pending:  Optional[dict] = None
    trades:   List[TradeRecord] = []
    equity:   List[dict]        = []
    blocked_today: set          = set()   # no re-entry
    last_date: str              = ""

    # Costs & slippage (make backtest results realistic, not optimistic).
    slip      = float(getattr(settings, "BACKTEST_SLIPPAGE_PCT", 0.0))
    cost_pct  = float(getattr(settings,
                  "BACKTEST_COST_PCT" if is_intra else "BACKTEST_COST_PCT_DELIVERY",
                  getattr(settings, "BACKTEST_COST_PCT", 0.0)))
    total_costs = 0.0   # accumulated brokerage+taxes+fees across all fills

    def _charge(value: float) -> float:
        """Cost for one fill of given rupee value; also accrues to total_costs."""
        nonlocal total_costs
        c = abs(value) * cost_pct / 100
        total_costs += c
        return c

    for i in range(WARMUP, len(df_ind)):
        row       = df_ind.iloc[i]
        ts        = df_ind.index[i]
        date_str  = str(ts)[:10]
        time_str  = str(ts)[11:16] if len(str(ts)) > 10 else "00:00"
        bar_open  = float(row["open"])
        bar_high  = float(row["high"])
        bar_low   = float(row["low"])
        bar_close = float(row["close"])

        # Reset daily re-entry block on new day
        if date_str != last_date:
            blocked_today.clear()
            last_date = date_str

        # ── Execute pending entry on bar open ─────────────────────────────
        if pending and pos is None:
            if bar_open <= 0:
                pending = None
            else:
                # Entry fills slightly ABOVE the bar open due to slippage.
                buy_fill, _ = _fill_prices(bar_open, slip)
                trade_amt  = min(risk.trade_amount,
                                 capital * settings.MAX_TRADE_PCT / 100)
                qty        = max(1, math.floor(trade_amt / buy_fill))
                # SL/T1/T2 are computed off the actual fill price, not the ideal open.
                sl, t1, t2 = (
                    round(buy_fill * (1 - risk.sl_pct / 100), 2),
                    round(buy_fill * (1 + risk.target1_pct / 100), 2),
                    round(buy_fill * (1 + risk.target2_pct / 100), 2),
                )
                gross = buy_fill * qty
                fee   = _charge(gross)
                if gross + fee > cash:
                    pending = None
                else:
                    pos  = Position(date_str, buy_fill, qty, sl, t1, t2,
                                    trail_sl=sl)
                    cash -= (gross + fee)
                    pending = None

        # ── Manage open position ──────────────────────────────────────────
        if pos:
            exit_price  = None
            exit_reason = None

            # Partial booking at Target 1
            if not pos.partial_booked and bar_high >= pos.target1:
                partial_qty   = max(1, pos.qty // 2)
                _, sell_fill  = _fill_prices(pos.target1, slip)   # sells fill lower
                proceeds      = sell_fill * partial_qty
                cash         += proceeds - _charge(proceeds)
                pos.qty      -= partial_qty
                pos.partial_booked = True
                pos.partial_qty    = partial_qty
                pos.partial_fill   = sell_fill
                # Activate trailing SL after partial booking.
                # FIX: raise the ACTUAL stop_loss here in the same step — not just
                # pos.trail_sl. The live scanner raises order.stop_loss immediately
                # on partial booking, but the backtest used to only bump pos.trail_sl
                # and relied on a separate bar_close-based update (which lags and uses
                # a different reference) to eventually propagate into pos.stop_loss.
                # That left the remaining half exposed to the original (often-below-
                # entry) stop for one or more bars, making backtests look safer than
                # live trading actually is. Keep both in lockstep with the scanner.
                new_sl        = round(pos.target1 * (1 - risk.trailing_sl_pct / 100), 2)
                pos.trail_sl  = max(pos.trail_sl, new_sl)
                if pos.trail_sl > pos.stop_loss:
                    pos.stop_loss = pos.trail_sl

            # Update trailing SL
            new_trail = round(bar_close * (1 - risk.trailing_sl_pct / 100), 2)
            if pos.partial_booked and new_trail > pos.trail_sl:
                pos.trail_sl = new_trail
                if pos.trail_sl > pos.stop_loss:
                    pos.stop_loss = pos.trail_sl

            # Full target hit OR stop hit. FIX (bug 5): when a SINGLE bar's range
            # spans BOTH the target and the stop, we cannot know which executed
            # first intrabar. The old code always checked target2 first and booked
            # the WIN, systematically overstating results. Assume the WORSE outcome
            # (stop-first) when both are inside the same bar — the conservative,
            # honest assumption for a backtest.
            hit_target = bar_high >= pos.target2
            hit_stop   = bar_low <= pos.stop_loss
            if hit_target and hit_stop:
                # ambiguous bar → take the stop (worse case)
                exit_price = pos.stop_loss
                exit_reason = "TRAILING_SL" if pos.partial_booked else "STOP_LOSS"
            elif hit_target:
                exit_price, exit_reason = pos.target2, "TARGET2"
            elif hit_stop:
                exit_price = pos.stop_loss
                exit_reason = "TRAILING_SL" if pos.partial_booked else "STOP_LOSS"

            # Intraday EOD exit
            if exit_price is None and is_intra:
                if time_str >= settings.INTRADAY_EXIT:
                    pnl_so_far = (bar_close - pos.entry_price) * pos.qty
                    if pnl_so_far < 0:
                        exit_price = bar_close
                        exit_reason = "EOD_LOSS"
                    elif time_str >= settings.INTRADAY_LAST_EXIT:
                        exit_price = bar_close
                        exit_reason = "EOD_FINAL"

            if exit_price is not None and pos.qty > 0:
                # Exit fills WORSE than the ideal trigger price (sells fill lower).
                # EOD exits at bar_close also slip; all sells use the same model.
                _, exit_fill = _fill_prices(exit_price, slip)
                proceeds     = exit_fill * pos.qty
                exit_fee     = _charge(proceeds)
                cash        += proceeds - exit_fee

                # P&L from ACTUAL fills (entry already slippage-adjusted; partial
                # booked at its real fill). Then subtract the round-trip costs that
                # apply to THIS trade so the reported pnl is net, not gross.
                partial_pnl = ((pos.partial_fill - pos.entry_price) * pos.partial_qty
                               if pos.partial_booked else 0)
                remain_pnl  = (exit_fill - pos.entry_price) * pos.qty
                # Approximate this trade's share of costs: entry fee on full qty +
                # exit fee + partial fee. We re-derive them from cost_pct on fills.
                entry_val   = pos.entry_price * (pos.qty + pos.partial_qty)
                partial_val = (pos.partial_fill * pos.partial_qty) if pos.partial_booked else 0
                trade_costs = (entry_val + proceeds + partial_val) * cost_pct / 100
                total_pnl   = partial_pnl + remain_pnl - trade_costs
                pnl_pct     = total_pnl / entry_val * 100 if entry_val else 0

                trades.append(TradeRecord(
                    entry_date=pos.entry_date, exit_date=date_str,
                    entry_price=round(pos.entry_price, 2),
                    exit_price=round(exit_fill, 2),
                    qty=pos.qty + pos.partial_qty,
                    side="BUY", pnl=round(total_pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    exit_reason=exit_reason,
                ))
                blocked_today.add(strategy.symbol)   # no re-entry today
                pos   = None

        # ── Check entry ───────────────────────────────────────────────────
        if pos is None and pending is None:
            # FIX: skip entry time filter for daily/weekly bars (no intraday time component)
            intraday_tfs = {"1min","3min","5min","15min","30min","1hr"}
            has_time     = tf_val in intraday_tfs and time_str and time_str != "00:00"
            # FIX: also enforce ENTRY_END (was defined but never used) so backtests
            # match live-scanner behaviour — no new entries outside the entry window.
            if has_time and not (settings.ENTRY_START <= time_str <= settings.ENTRY_END):
                pass
            elif strategy.symbol in blocked_today:
                pass    # no re-entry today
            else:
                try:
                    window = df_ind.iloc[max(0, i-50): i+1]
                    result = evaluate_signal(strategy, window)
                    if result["signal"] == "BUY" and result["confidence"] >= 60:
                        pending = {"signal": "BUY"}
                except Exception:
                    pass

        port_val = cash + (pos.qty * bar_close if pos else 0)
        equity.append({"date": date_str, "value": round(port_val, 2)})

    # Close remaining position
    if pos:
        last_price = float(df_ind["close"].iloc[-1])
        _, exit_fill = _fill_prices(last_price, slip)
        proceeds   = exit_fill * pos.qty
        cash      += proceeds - _charge(proceeds)
        partial_pnl = ((pos.partial_fill - pos.entry_price) * pos.partial_qty
                       if pos.partial_booked else 0)
        remain_pnl  = (exit_fill - pos.entry_price) * pos.qty
        entry_val   = pos.entry_price * (pos.qty + pos.partial_qty)
        partial_val = (pos.partial_fill * pos.partial_qty) if pos.partial_booked else 0
        trade_costs = (entry_val + proceeds + partial_val) * cost_pct / 100
        total_pnl   = partial_pnl + remain_pnl - trade_costs
        trades.append(TradeRecord(
            entry_date=pos.entry_date, exit_date=str(df_ind.index[-1])[:10],
            entry_price=round(pos.entry_price, 2), exit_price=round(exit_fill, 2),
            qty=pos.qty + pos.partial_qty, side="BUY",
            pnl=round(total_pnl, 2),
            pnl_pct=round(total_pnl / entry_val * 100 if entry_val else 0, 2),
            exit_reason="END",
        ))

    final_cap    = cash
    total_return = (final_cap - capital) / capital * 100

    try:
        yrs  = max((datetime.fromisoformat(req.end_date) -
                    datetime.fromisoformat(req.start_date)).days / 365.25, 0.01)
        cagr = ((final_cap / capital) ** (1 / yrs) - 1) * 100
    except Exception:
        cagr = total_return

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    peak = capital; max_dd = 0.0
    for e in equity:
        peak   = max(peak, e["value"])
        max_dd = max(max_dd, (peak - e["value"]) / peak * 100)

    ann_factor = _ANN.get(tf_val, 252)
    if len(equity) > 1:
        vals   = pd.Series([e["value"] for e in equity])
        rets   = vals.pct_change().dropna()
        sharpe = (rets.mean() / rets.std() * (ann_factor ** 0.5)) if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0

    gross_p = sum(t.pnl for t in wins)
    gross_l = abs(sum(t.pnl for t in losses))
    pf      = gross_p / gross_l if gross_l > 0 else 9999.0
    pnl_pcts = [t.pnl_pct for t in trades]

    # Strategy score
    win_rate  = len(wins) / len(trades) * 100 if trades else 0
    score, grade = _score(win_rate, cagr, max_dd, sharpe, pf)

    step   = max(1, len(equity) // 120)
    eq_out = equity[::step]

    return BacktestResult(
        symbol=req.symbol, strategy_name=strategy.name,
        start_date=req.start_date, end_date=req.end_date,
        initial_capital=round(capital, 2), final_capital=round(final_cap, 2),
        total_return_pct=round(total_return, 2), cagr_pct=round(cagr, 2),
        max_drawdown_pct=round(max_dd, 2), win_rate_pct=round(win_rate, 2),
        total_trades=len(trades), winning_trades=len(wins),
        losing_trades=len(losses), sharpe_ratio=round(sharpe, 2),
        profit_factor=round(pf, 2),
        avg_trade_pct=round(sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0, 2),
        best_trade_pct=round(max(pnl_pcts) if pnl_pcts else 0, 2),
        worst_trade_pct=round(min(pnl_pcts) if pnl_pcts else 0, 2),
        score=score, score_grade=grade,
        equity_curve=eq_out, trades=trades,
    )


def _score(win_rate, cagr, max_dd, sharpe, pf) -> tuple:
    s  = 0.0
    s += min(30, win_rate * 0.4)
    s += min(25, max(0, cagr) * 0.8)
    s += min(20, max(0, 20 - max_dd))
    s += min(15, sharpe * 5)
    s += min(10, min(pf, 5) * 2)
    # FIX: sharpe*5 has no floor — a bad sharpe ratio (e.g. -8) can swing this
    # term to -40, dragging the total score deeply negative. A "−19.8/100"
    # score is meaningless to a user; clamp to the documented 0-100 scale.
    s  = max(0.0, min(100.0, round(s, 1)))
    if s >= 80: grade = "A+"
    elif s >= 70: grade = "A"
    elif s >= 60: grade = "B"
    elif s >= 50: grade = "C"
    else: grade = "D"
    return s, grade
