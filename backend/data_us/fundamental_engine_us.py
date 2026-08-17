"""
Rush Algo - US Fundamental Scoring Engine (6 categories, 100 points)
=====================================================================
Scores US companies on QUALITY only. Deliberately separate from the NSE engine
(data/fundamental_engine.py), which leans on India-specific fields (promoter
holding) and different accounting conventions.

CATEGORIES (100 pts total):
    Profitability        25   ROE, net margin, operating margin
    Financial health     20   D/E, current ratio, debt TREND over time
    Growth quality       20   revenue CAGR, EPS growth, growth CONSISTENCY
    Earnings consistency 15   how many of the last N years were profitable
    Cash generation      10   real FCF and FCF margin (from cash flow stmt)
    Valuation            10   P/E and P/S sanity

WHY TREND/CONSISTENCY MATTER: a single-snapshot ratio can't tell a company with
10 straight profitable years apart from one that just turned profitable this
quarter. The multi-year statement history makes that distinction possible, and
it's the main thing separating this from a naive 4-ratio score.

SENTIMENT IS DELIBERATELY EXCLUDED: insider transactions and analyst
recommendations are fetched and stored, but do NOT feed the score. Mixing
sentiment into a fundamental quality number makes the number mean two things at
once, and analyst consensus in particular is a weak predictor. They're kept
alongside for display/analysis instead.

MISSING DATA: each category degrades gracefully - a category with no usable
inputs contributes 0 rather than crashing or silently assuming a value. The
returned breakdown shows exactly what each category earned, so a low score can
always be traced to a real cause (weak fundamentals vs missing data).
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _safe(v) -> Optional[float]:
    """Coerce to float, or None. Guards against strings/None/NaN in API data."""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f      # NaN check
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Category scorers - each returns (points_earned, max_points, note)
# ---------------------------------------------------------------------------
def _score_profitability(m: dict) -> tuple:
    """25 pts: ROE (12), net margin (7), operating margin (6)."""
    pts = 0.0
    roe = _safe(m.get("roe"))
    if roe is not None:
        # 0% -> 0 pts, >=30% -> full. Capped so a 200% ROE (buyback-driven,
        # like AAPL) doesn't dominate purely on magnitude.
        pts += _clamp(roe / 30.0) * 12

    nm = _safe(m.get("net_margin"))
    if nm is not None:
        pts += _clamp(nm / 20.0) * 7        # 20%+ net margin = full marks

    om = _safe(m.get("operating_margin"))
    if om is not None:
        pts += _clamp(om / 25.0) * 6        # 25%+ operating margin = full

    return round(pts, 2), 25, "ROE + margins"


def _score_financial_health(m: dict) -> tuple:
    """20 pts: D/E (10), current ratio (5), debt trend (5)."""
    pts = 0.0
    de = _safe(m.get("debt_to_equity"))
    if de is not None:
        # D/E 0 -> full, >=2.0 -> 0. Inverted: leverage is risk.
        pts += _clamp(1 - (de / 2.0)) * 10

    cr = _safe(m.get("current_ratio"))
    if cr is not None:
        # <1 is a liquidity warning; 2.0+ is comfortable.
        pts += _clamp((cr - 0.5) / 1.5) * 5

    # Debt TREND: is total debt shrinking or growing over the available years?
    trend = _safe(m.get("debt_trend_pct"))   # negative = debt falling = good
    if trend is not None:
        # -50% (halved debt) -> full, +50% (debt up 50%) -> 0
        pts += _clamp((50.0 - trend) / 100.0) * 5

    return round(pts, 2), 20, "leverage + liquidity + debt trend"


# Growth above this earns diminishing returns rather than a hard cutoff. A hard
# cap would score 60% and 500% identically (losing real signal); ignoring the
# problem lets a base-effect artifact - a tiny prior-year denominator, e.g. the
# 108,336% seen on IBULLSLTD - outrank a company compounding a steady 25% while
# also being profitable and cash-generative.
_GROWTH_FULL_MARKS = 25.0      # sustained ~25% growth already earns full credit


def _growth_curve(pct: float) -> float:
    """Map a growth percentage to 0..1 with diminishing returns past full marks.

    0% -> 0, 25% -> 1.0, and beyond that the curve flattens hard: 100% only
    reaches ~1.0 (capped), so an implausible 500% cannot buy more points than
    genuine, sustainable growth."""
    if pct is None:
        return 0.0
    if pct <= 0:
        return 0.0
    return _clamp(pct / _GROWTH_FULL_MARKS)


def _pick_growth_rate(m: dict) -> tuple:
    """Choose the most trustworthy growth figure available, and say which.

    Priority, most reliable first:
      1. CAGR derived from actual filings, but ONLY with >=3 years of history -
         a "5-year CAGR" computed without 5 years of data is fiction. This is
         what broke on a 2024 spin-off: Finnhub's revenueGrowth5Y returned
         -29.4% for a company that didn't exist 5 years ago, and because that
         value wasn't None it beat the real +528% current growth and scored 0.
      2. Latest year-over-year growth - current and directly measured.
      3. A vendor multi-year aggregate, only when history actually backs it.
    """
    years = _safe(m.get("years_of_history")) or 0
    cagr = _safe(m.get("revenue_cagr"))
    yoy = _safe(m.get("revenue_growth"))

    if cagr is not None and years >= 3:
        return cagr, f"CAGR over {int(years)}y of filings"
    if yoy is not None:
        return yoy, "latest YoY (insufficient filing history for CAGR)"
    if cagr is not None:
        return cagr, "vendor CAGR (unverified history)"
    return None, "no growth data"


def _score_growth(m: dict) -> tuple:
    """20 pts: revenue growth (9), EPS growth (6), growth consistency (5)."""
    pts = 0.0
    rate, source = _pick_growth_rate(m)
    if rate is not None:
        pts += _growth_curve(rate) * 9

    eps_g = _safe(m.get("profit_growth"))
    if eps_g is not None:
        pts += _growth_curve(eps_g) * 6

    # CONSISTENCY: fraction of years where revenue grew vs the prior year.
    consistency = _safe(m.get("revenue_growth_consistency"))   # 0..1
    if consistency is not None:
        pts += _clamp(consistency) * 5

    return round(pts, 2), 20, source


def _score_earnings_consistency(m: dict) -> tuple:
    """15 pts: what fraction of available years were profitable.
    This is the category that a single-snapshot score simply cannot express."""
    frac = _safe(m.get("profitable_years_frac"))   # 0..1
    if frac is None:
        return 0.0, 15, "no multi-year earnings history"
    yrs = m.get("years_of_history") or 0
    # Require a meaningful history before awarding full marks - 2 profitable
    # years out of 2 is not the same evidence as 10 out of 10.
    depth = _clamp(yrs / 8.0)
    return round(_clamp(frac) * depth * 15, 2), 15, f"profitable {frac:.0%} of {yrs}y"


def _score_cash_generation(m: dict) -> tuple:
    """10 pts: FCF positive (5) + FCF margin (5). Cash is harder to manipulate
    than accounting earnings, so it's a useful independent quality check."""
    pts = 0.0
    fcf = _safe(m.get("fcf"))
    if fcf is not None:
        pts += 5.0 if fcf > 0 else 0.0

    fcf_margin = _safe(m.get("fcf_margin"))
    if fcf_margin is not None:
        pts += _clamp(fcf_margin / 15.0) * 5      # 15%+ FCF margin = full

    return round(pts, 2), 10, "FCF + FCF margin"


def _score_valuation(m: dict) -> tuple:
    """10 pts: P/E (6) + P/S (4). Not 'cheap = good' - extreme lows often signal
    distress. Scores a sane band highest, penalising both ends."""
    pts = 0.0
    pe = _safe(m.get("pe_ratio"))
    if pe is not None and pe > 0:
        if pe <= 45:
            # Triangular around ~18: distressed-cheap and bubble-expensive both score low
            pts += _clamp(1 - abs(pe - 18) / 25.0) * 6

    ps = _safe(m.get("ps_ratio"))
    if ps is not None and ps > 0:
        if ps <= 15:
            pts += _clamp(1 - abs(ps - 3) / 9.0) * 4

    return round(pts, 2), 10, "P/E + P/S sanity"


# ---------------------------------------------------------------------------
# Data-integrity check
# ---------------------------------------------------------------------------
# Deliberately separates two very different situations:
#
#   A) UNVERIFIABLE - short filing history, missing fields. The company simply
#      doesn't EARN those points (category scores 0). NO penalty: a recent IPO
#      or spin-off with thin filings is not suspicious, just young, and
#      penalising it would confuse "we can't check this" with "this looks wrong".
#
#   B) CONTRADICTORY - numbers that disagree with each other. THAT is evidence
#      something is off, and it gets a real (capped) penalty with the reason
#      recorded, so a low score is always traceable rather than mysterious.
#
# The penalty is capped so data hygiene can never dominate the score - it should
# still mostly reflect business quality.
_MAX_INTEGRITY_PENALTY = 5.0

# Above this, a growth figure is almost always a base effect (a near-zero prior
# year), not real performance - e.g. the 108,336% seen on IBULLSLTD.
_IMPLAUSIBLE_GROWTH_PCT = 300.0


def check_data_integrity(m: dict) -> tuple:
    """Return (penalty_points, [reasons]). Penalty is 0.._MAX_INTEGRITY_PENALTY."""
    penalty = 0.0
    reasons: List[str] = []

    cagr = _safe(m.get("revenue_cagr"))
    yoy = _safe(m.get("revenue_growth"))
    years = _safe(m.get("years_of_history")) or 0

    # 1) Opposite-sign growth: multi-year says shrinking, current says booming
    #    (or vice versa). Exactly the Nebius case: CAGR -29.4% vs YoY +528%.
    if cagr is not None and yoy is not None and years >= 3:
        if (cagr < -5 and yoy > 50) or (cagr > 50 and yoy < -25):
            penalty += 3.0
            reasons.append(f"growth contradiction: CAGR {cagr:.1f}% vs YoY {yoy:.1f}%")

    # 2) Implausible growth magnitude - base-effect artifact, not performance.
    for label, val in (("revenue", yoy), ("EPS", _safe(m.get("profit_growth")))):
        if val is not None and val > _IMPLAUSIBLE_GROWTH_PCT:
            penalty += 2.0
            reasons.append(f"implausible {label} growth {val:,.0f}% (likely base effect)")
            break

    # 3) Margin outside any plausible range - suggests a mis-parsed line item
    #    rather than a real business result.
    nm = _safe(m.get("net_margin"))
    if nm is not None and (nm > 90 or nm < -500):
        penalty += 2.0
        reasons.append(f"implausible net margin {nm:.1f}%")

    # 4) Reported vs vendor revenue disagreement >30% - one of them is wrong.
    rep_rev = _safe(m.get("reported_revenue"))
    vendor_rev = _safe(m.get("vendor_revenue"))
    if rep_rev and vendor_rev and rep_rev > 0 and vendor_rev > 0:
        diff = abs(rep_rev - vendor_rev) / max(rep_rev, vendor_rev)
        if diff > 0.30:
            penalty += 2.0
            reasons.append(f"revenue mismatch {diff*100:.0f}% between sources")

    return min(penalty, _MAX_INTEGRITY_PENALTY), reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_fundamental_score_us(fd, extras: Optional[dict] = None) -> float:
    """Return a 0-100 quality score. `fd` is a FundamentalData; `extras` is the
    optional US-only derived-metrics dict (margins, CAGR, FCF, consistency)
    computed from the multi-year statements."""
    score, _ = compute_score_with_breakdown_us(fd, extras)
    return score


def compute_score_with_breakdown_us(fd, extras: Optional[dict] = None) -> tuple:
    """Return (score, breakdown). The breakdown makes a score auditable - you
    can always see which category earned what, so a low score is traceable to a
    real weakness rather than to silently missing data."""
    m = {
        "roe":            getattr(fd, "roe", None),
        "debt_to_equity": getattr(fd, "debt_to_equity", None),
        "current_ratio":  getattr(fd, "current_ratio", None),
        "revenue_growth": getattr(fd, "revenue_growth", None),
        "profit_growth":  getattr(fd, "profit_growth", None),
        "pe_ratio":       getattr(fd, "pe_ratio", None),
    }
    if extras:
        m.update(extras)

    parts = [
        ("profitability",        _score_profitability(m)),
        ("financial_health",     _score_financial_health(m)),
        ("growth",               _score_growth(m)),
        ("earnings_consistency", _score_earnings_consistency(m)),
        ("cash_generation",      _score_cash_generation(m)),
        ("valuation",            _score_valuation(m)),
    ]

    total = 0.0
    breakdown: Dict[str, dict] = {}
    for name, (pts, mx, note) in parts:
        total += pts
        breakdown[name] = {"points": pts, "max": mx, "note": note}

    # Data-integrity penalty: only for CONTRADICTORY data, never for merely
    # missing data (that already scores 0 in its own category).
    penalty, reasons = check_data_integrity(m)
    if penalty:
        total -= penalty
        breakdown["data_integrity"] = {
            "points": -round(penalty, 2), "max": 0,
            "note": "; ".join(reasons),
        }

    # Never let data-quality alone push a score below zero.
    return round(max(total, 0.0), 1), breakdown
