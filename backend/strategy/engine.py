"""
Rush Algo — Strategy Engine
Evaluates entry conditions + multi-timeframe (MTF) confirmation.
MTF: primary signal on 5min, confirmed by 15min, 30min, 1hr trend.
"""
from __future__ import annotations
import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from models.schemas import Condition, Signal, Strategy
from indicators.library import compute_all, INDICATOR_COLS

logger = logging.getLogger(__name__)


def _safe(v) -> float:
    """Return float, 0.0 for NaN/None/pd.NA."""
    if v is None: return 0.0
    try:
        f = float(v)
        return 0.0 if (f != f) else f   # f != f is True only for NaN
    except (TypeError, ValueError):
        return 0.0


def _get_col(name: str) -> str:
    """Map frontend indicator name → DataFrame column name."""
    return INDICATOR_COLS.get(name, name.lower().replace(" ", "_"))


def _resolve_value(val: str, row: pd.Series) -> float:
    """Resolve value string — could be a number or another indicator name."""
    v = val.strip()
    col = _get_col(v)
    if col in row.index:
        raw = row.get(col)
        # FIX: must preserve real NaN here, NOT call _safe() — _safe() converts
        # NaN to 0.0, which silently defeats every downstream np.isnan() guard
        # and lets corrupt/missing data masquerade as a valid "0" value.
        return np.nan if pd.isna(raw) else float(raw)
    try:
        return float(v)
    except ValueError:
        return np.nan


def _eval_condition(c: Condition, row: pd.Series,
                    prev: Optional[pd.Series]) -> Tuple[bool, str]:
    ind_col   = _get_col(c.indicator)
    # FIX: was `_safe(row.get(ind_col, np.nan))` — _safe() silently turns NaN
    # into 0.0, which made the np.isnan() guard below permanently unreachable.
    # A fully-missing/corrupt bar (all-NaN row) would then evaluate conditions
    # like "RSI < 30" as 0 < 30 = True, generating a false BUY at 100%
    # confidence on garbage data. Preserve real NaN through this path.
    raw_ind   = row.get(ind_col, np.nan)
    ind_val   = np.nan if pd.isna(raw_ind) else float(raw_ind)
    ref_val   = _resolve_value(c.value, row)
    comp      = c.comparator.value.lower()

    if np.isnan(ind_val) or np.isnan(ref_val):
        return False, f"{c.indicator} N/A"

    if comp == "greater_than":
        ok = ind_val > ref_val
        return ok, f"{c.indicator}({ind_val:.2f}) > {ref_val:.2f}"
    if comp == "less_than":
        ok = ind_val < ref_val
        return ok, f"{c.indicator}({ind_val:.2f}) < {ref_val:.2f}"
    if comp == "equals":
        ok = abs(ind_val - ref_val) < 0.01
        return ok, f"{c.indicator}({ind_val:.2f}) = {ref_val:.2f}"
    if comp == "crosses_above":
        if prev is None: return False, "no prev bar"
        raw_prev_ind = prev.get(ind_col, np.nan)
        prev_ind = np.nan if pd.isna(raw_prev_ind) else float(raw_prev_ind)  # FIX: same NaN-preserve
        prev_ref = _resolve_value(c.value, prev)
        if np.isnan(prev_ind) or np.isnan(prev_ref):
            return False, f"{c.indicator} N/A (prev bar)"
        ok = (ind_val > ref_val) and (prev_ind <= prev_ref)
        return ok, f"{c.indicator} crossed above {ref_val:.2f}"
    if comp == "crosses_below":
        if prev is None: return False, "no prev bar"
        raw_prev_ind = prev.get(ind_col, np.nan)
        prev_ind = np.nan if pd.isna(raw_prev_ind) else float(raw_prev_ind)  # FIX: same NaN-preserve
        prev_ref = _resolve_value(c.value, prev)
        if np.isnan(prev_ind) or np.isnan(prev_ref):
            return False, f"{c.indicator} N/A (prev bar)"
        ok = (ind_val < ref_val) and (prev_ind >= prev_ref)
        return ok, f"{c.indicator} crossed below {ref_val:.2f}"

    return False, f"unknown comparator: {comp}"


def evaluate_signal(strategy: Strategy, df_ind: pd.DataFrame) -> dict:
    """
    Evaluate entry conditions on the latest bar.
    Returns dict with signal, confidence, reasons, indicator snapshot.
    """
    if len(df_ind) < 2:
        return {"signal": Signal.WATCH.value, "confidence": 0.0,
                "reasons": ["Insufficient data"], "indicators": {}}

    row  = df_ind.iloc[-1]
    prev = df_ind.iloc[-2]
    conds = strategy.entry_conditions

    if not conds:
        return {"signal": Signal.WATCH.value, "confidence": 0.0,
                "reasons": ["No conditions defined"], "indicators": {}}

    results = [_eval_condition(c, row, prev) for c in conds]
    joins   = [c.join.upper() for c in conds]

    # Combine with AND/OR chain
    final = results[0][0]
    for i in range(1, len(results)):
        op = joins[i] if i < len(joins) else "AND"
        final = (final or results[i][0]) if op == "OR" else (final and results[i][0])

    n_met = sum(1 for ok, _ in results if ok)

    # FIX (bugs 1-3): confidence must agree with the actual signal logic, not the
    # raw fraction of conditions met. Previously confidence = n_met/total, which
    # for an OR strategy could be e.g. 33% even when the signal validly fired —
    # and the backtest + live scanner both gate entries on `confidence >= 60`,
    # so legitimate OR-based BUY signals were SILENTLY rejected everywhere.
    # Now: a fired signal starts at a confident baseline and rises with how many
    # conditions agree; a non-fired signal reflects how close it was.
    if final:
        # Signal is valid. Baseline 60 (the entry gate) so a real signal is never
        # rejected by its own confidence, plus a bonus for extra confirming conditions.
        extra = (n_met - 1) / len(results) if len(results) > 1 else 0
        confidence = round(min(60 + extra * 40, 100.0), 1)
    else:
        confidence = round(n_met / len(results) * 100, 1) if results else 0.0

    # ADX boost for trending market
    adx_val = _safe(row.get("adx", 0))
    if adx_val > 25:
        confidence = min(confidence * 1.1, 98.0)

    signal  = Signal.BUY.value if final else Signal.WATCH.value
    reasons = [r for ok, r in results if ok] if final else [r for ok, r in results if not ok]

    def _snap(col, digits=2):
        return round(_safe(row.get(col, 0)), digits)

    indicators = {
        "close": _snap("close"), "rsi": _snap("rsi_14"),
        "ema_20": _snap("ema_20"), "ema_50": _snap("ema_50"),
        "macd_hist": _snap("macd_hist", 4), "adx": _snap("adx"),
        "supertrend_dir": _snap("supertrend_dir", 0),
        "vwap": _snap("vwap"), "bb_pct": _snap("bb_pct", 3),
        "stoch_k": _snap("stoch_k"), "mfi": _snap("mfi_14"),
    }

    return {"signal": signal, "confidence": confidence,
            "reasons": reasons, "indicators": indicators,
            "price": _safe(row.get("close", 0))}


def check_mtf_confirmation(strategy: Strategy, fetch_fn) -> Tuple[bool, str]:
    """
    Confirm signal across 15min, 30min, 1hr timeframes.
    Checks that EMA(20) > EMA(50) (uptrend) and SuperTrend is bullish.
    Returns (confirmed: bool, reason: str).
    """
    if not strategy.mtf.enabled:
        return True, "MTF disabled"

    confirm_tfs = strategy.mtf.confirm_tfs
    confirmations = []

    for tf in confirm_tfs:
        try:
            df = fetch_fn(strategy.symbol, tf)
            if len(df) < 50:
                confirmations.append((False, f"{tf}: insufficient data"))
                continue
            df_ind = compute_all(df)
            last   = df_ind.iloc[-1]
            ema20  = _safe(last.get("ema_20", 0))
            ema50  = _safe(last.get("ema_50", 0))
            st_dir = _safe(last.get("supertrend_dir", 0))
            adx_v  = _safe(last.get("adx", 0))

            trend_up = (ema20 > ema50) and (st_dir > 0)
            trending = adx_v > 20
            confirmed = trend_up and trending
            confirmations.append((confirmed, f"{tf}: EMA20{'>' if ema20>ema50 else '<'}EMA50 ST={'Bull' if st_dir>0 else 'Bear'} ADX={adx_v:.0f}"))
        except Exception as exc:
            confirmations.append((False, f"{tf}: error — {exc}"))

    if not confirmations:
        return True, "No confirmation timeframes available"

    confirmed_count = sum(1 for ok, _ in confirmations if ok)
    total           = len(confirmations)
    reasons         = [r for _, r in confirmations]

    if strategy.mtf.require_all:
        passed = confirmed_count == total
    else:
        # FIX (bug 10): a true majority is "more than half". The old
        # `total//2 + 1` over-counted for even totals (e.g. 2 TFs required 2,
        # i.e. ALL, not a majority). Use strictly-greater-than-half instead.
        passed = confirmed_count > total / 2

    return passed, f"MTF {confirmed_count}/{total}: " + " | ".join(reasons)
