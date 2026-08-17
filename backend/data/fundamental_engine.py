"""
Rush Algo — comprehensive fundamental scoring engine.

Design principle (per Rush's instruction): use EVERY fundamental segment IndianAPI
provides, rank PURELY on fundamentals, and never throw raw data away. Price-action
(currentPrice, technicals, beta, % changes) and news are deliberately EXCLUDED from
the fundamental rank — including them would corrupt a "purely fundamental" score —
but they're retained in the raw blob for the deep-dive dashboard.

The score is built as SIX weighted category scores, each computed from its relevant
raw fields, then combined. This avoids the failure mode of throwing 80 raw fields
into one flat sum (noise + double-counting). Every piece of data is used, once, in
the category where it belongs — the way real quality models (Piotroski, Greenblatt,
MSCI Quality) are built.

  1. Profitability     — ROE, ROA, ROI, margins (gross/op/net/pretax), profit scale
  2. Growth            — revenue/EPS/EBITDA growth (multi-year + self-computed YoY)
  3. Financial Strength— debt/equity, interest coverage, current/quick ratio, net debt
  4. Cash Quality      — FCF, FCF/revenue, operating cash flow vs net income (accruals)
  5. Valuation         — PE, PB, PS, PEG, price/FCF, dividend yield (cheaper = better)
  6. Mgmt & Ownership  — promoter holding LEVEL and TREND, payout, asset/inv turnover

Each category returns 0-100. Final = weighted blend. Weights carry a Buffett-style
quality tilt (profitability + cash quality + strength weighted above valuation).

This module is pure functions over the raw IndianAPI dict — no network, no I/O — so
it can be re-run over stored _raw blobs at any time to re-score the whole universe
WITHOUT re-fetching (which is exactly how we'll iterate when Rush gives feedback).
"""
from __future__ import annotations
from typing import Optional


# ───────────────────────── helpers ─────────────────────────

def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s in ("-", "--", "N/A", "NA", "null", "None"):
        return None
    cleaned = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    if cleaned in ("", "-", ".", "-.", ".-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _km(raw: dict, section: str) -> list:
    km = raw.get("keyMetrics") or {}
    arr = km.get(section)
    return arr if isinstance(arr, list) else []


def _kmv(raw: dict, section: str, *keys) -> Optional[float]:
    """Fetch a value by key from a keyMetrics section (exact then loose)."""
    items = _km(raw, section)
    for it in items:
        if isinstance(it, dict):
            k = str(it.get("key", "")).lower().rstrip(")")
            for want in keys:
                if k == want.lower():
                    return _num(it.get("value"))
    for it in items:
        if isinstance(it, dict):
            k = str(it.get("key", "")).lower()
            for want in keys:
                if want.lower() in k:
                    return _num(it.get("value"))
    return None


def _annual_series(raw: dict) -> list:
    """Return list of {fy, rev, ni, oi, ocf, capex, equity, debt} oldest→newest (₹cr)."""
    rows = []
    for blk in (raw.get("financials") or []):
        if not isinstance(blk, dict) or blk.get("Type") != "Annual":
            continue
        fy = str(blk.get("FiscalYear") or "")
        fmap = blk.get("stockFinancialMap") or {}
        inc = {str(i.get("key", "")).lower(): _num(i.get("value")) for i in (fmap.get("INC") or []) if isinstance(i, dict)}
        bal = {str(i.get("key", "")).lower(): _num(i.get("value")) for i in (fmap.get("BAL") or []) if isinstance(i, dict)}
        cas = {str(i.get("key", "")).lower(): _num(i.get("value")) for i in (fmap.get("CAS") or []) if isinstance(i, dict)}
        if not fy:
            continue
        rows.append({
            "fy": fy,
            "rev": inc.get("revenue") or inc.get("totalrevenue"),
            "ni": inc.get("netincome"),
            "oi": inc.get("operatingincome"),
            "gross": inc.get("grossprofit"),
            "ocf": cas.get("cashfromoperatingactivities"),
            "capex": cas.get("capitalexpenditures"),
            "equity": bal.get("totalequity"),
            "debt": bal.get("totaldebt"),
        })
    rows.sort(key=lambda r: r["fy"])
    return rows


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _scale(x, lo, hi):
    """Linear-scale x in [lo,hi] to [0,100], clamped. Handles None."""
    if x is None:
        return None
    if hi == lo:
        return 50.0
    return _clamp((x - lo) / (hi - lo) * 100.0)


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _avg_complete(vals, expected=None, floor=0.5):
    """
    Average the non-None parts, but apply a COMPLETENESS HAIRCUT so a category can't
    claim a high score when much of its underlying data is missing.

    Why: MOBILISE scored 100 on Cash Quality with FCF = None — it hit full marks on
    the couple of signals it *did* have, while its most important input was absent.
    A category built from 3 inputs but scored on only 1 shouldn't be trusted as much
    as one scored on all 3.

    `expected` = how many inputs the category is designed to use (defaults to len(vals)).
    If fewer than that are present, we scale the score down toward 0 by the fraction
    present — but never below `floor` coverage's worth, so a genuinely data-light but
    real stock isn't nuked to zero. Concretely: coverage = present/expected, and the
    score is multiplied by max(coverage, floor)-normalised weighting.
    """
    present = [v for v in vals if v is not None]
    if not present:
        return None
    base = sum(present) / len(present)
    exp = expected or len(vals)
    if exp <= 0:
        return base
    coverage = len(present) / exp            # 1.0 = all inputs present
    if coverage >= 1.0:
        return base
    # Haircut: interpolate the multiplier between `floor` (no data) and 1.0 (full).
    mult = floor + (1.0 - floor) * coverage
    return base * mult


# ───────────────────────── category scorers ─────────────────────────
# Each returns (score_0_100 or None, detail_dict). detail_dict feeds the dashboard.

def _extract_revenue_cr(raw: dict):
    """Latest annual revenue in ₹ crore from financials[] (division-free source)."""
    best_fy, best_rev = None, None
    for blk in (raw.get("financials") or []):
        if not isinstance(blk, dict) or blk.get("Type") != "Annual":
            continue
        fy = str(blk.get("FiscalYear") or "")
        inc = (blk.get("stockFinancialMap") or {}).get("INC") or []
        for it in inc:
            if isinstance(it, dict) and str(it.get("key", "")).lower() in ("revenue", "totalrevenue"):
                rev = _num(it.get("value"))
                if rev is not None and (best_fy is None or fy > best_fy):
                    best_fy, best_rev = fy, rev
    return best_rev


def _corroborated_roe(roe, roa, de, revenue_cr):
    """
    Return an ROE value for SCORING that dampens implausibly-high ROE unless it's
    corroborated by independent evidence. A genuinely elite business earns ~25-40%
    ROE sustainably; ROE far above that is often an ARTIFACT — a tiny equity base, or
    leverage inflating the ratio, or a one-off. Validated on real data: WAAREEINDO
    shows ROE 150%, KSOLVES 137%, VIVIDEL 110% — not real sustainable returns.

    Corroboration (multi-signal — an extreme ROE must be EARNED, not just reported):
      • ROA backs it up — real profitability shows in return on ASSETS too, which
        leverage and thin equity can't fake. High ROE + mediocre ROA = leverage/base
        artifact. (Strongest single corroborator.)
      • Leverage is low — high ROE on high debt is mechanically inflated, not quality.
      • Real revenue scale — thin-revenue companies produce unstable, easily-distorted
        ratios.

    IMPORTANT: the profitability scale maxes ROE credit at 30%, so merely trimming a
    150% ROE to 98% would still score 100/100 — no effect. So instead, an UNCORROBORATED
    extreme ROE is treated as SUSPICIOUS and scored DOWN INTO the believable band (or
    below it), where it actually moves the score. A well-corroborated high ROE (JSLL,
    ICICIAMC: strong ROA, low debt, real scale) is left at/above the cap so it still
    maxes — as it should.

    Returns an ROE value to feed into _scale(·, 5, 30). Below the ceiling → untouched.
    """
    if roe is None:
        return None
    CEILING = 45.0
    if roe <= CEILING:
        return roe                     # believable — no dampening

    # Count corroborators (0..3). ROA is the strongest; weight it double.
    corr = 0.0
    if roa is not None and roa >= 12:  corr += 2.0   # strong asset returns (hard to fake)
    elif roa is not None and roa >= 8: corr += 1.0   # decent asset returns
    if de is not None and de < 0.5:    corr += 1.0   # not leverage-inflated
    if revenue_cr is not None and revenue_cr >= 1000: corr += 1.0  # real scale
    # max corr = 4.0 (roa 2 + de 1 + scale 1)

    if corr >= 3.0:
        # Well corroborated (e.g. JSLL, ICICIAMC): genuine elite business. Keep it
        # at the top of the scale so it maxes profitability as it deserves.
        return roe
    # Not (or weakly) corroborated: treat the extreme ROE as suspect. Score it as a
    # believable ROE scaled by how much corroboration exists — from ~18% (no evidence
    # at all) up toward ~35% (almost-but-not-quite corroborated). This lands at/below
    # the 30% cap so the artifact actually loses profitability credit.
    #   corr 0 → 18%   corr 1 → ~24%   corr 2 → ~30%   corr 2.5→ ~33%
    return 18.0 + corr * 6.0


def score_profitability(raw: dict) -> tuple:
    roe = _kmv(raw, "mgmtEffectiveness", "returnOnAverageEquityTrailing12Month",
               "returnOnAverageEquityMostRecentFiscalYear")
    roa = _kmv(raw, "mgmtEffectiveness", "returnOnAverageAssetsTrailing12Month",
               "returnOnAverageAssetsMostRecenFiscalYear")
    roi = _kmv(raw, "mgmtEffectiveness", "returnOnInvestmentTrailing12Month")
    op_margin = _kmv(raw, "margins", "operatingMarginTrailing12Month", "operatingMargin5YearAverage")
    net_margin = _kmv(raw, "margins", "netProfitMarginPercentTrailing12Month", "netProfitMargin5YearAverage")
    gross_margin = _kmv(raw, "margins", "grossMarginTrailing12Month", "grossMargin5YearAverage")

    # Corroborate an extreme ROE before scoring (dampens small-base/leverage artifacts).
    de = _kmv(raw, "financialstrength", "totalDebtPerTotalEquityMostRecentQuarter",
              "totalDebtPerTotalEquityMostRecentFiscalYear")
    rev_cr = _extract_revenue_cr(raw)
    roe_scored = _corroborated_roe(roe, roa, de, rev_cr)

    parts = [
        _scale(roe_scored, 5, 30),     # 5%→0, 30%+→100 (using corroborated ROE)
        _scale(roa, 2, 15),
        _scale(roi, 4, 20),
        _scale(op_margin, 5, 25),
        _scale(net_margin, 3, 20),
        _scale(gross_margin, 15, 50),
    ]
    s = _avg(parts)
    return s, {"roe": roe, "roe_scored": roe_scored, "roa": roa, "roi": roi,
               "op_margin": op_margin, "net_margin": net_margin, "gross_margin": gross_margin}


def score_growth(raw: dict) -> tuple:
    rev_ttm = _kmv(raw, "growth", "revenueChangePercentTTMPOverTTM")
    rev_5y = _kmv(raw, "growth", "revenueGrowthRate5Year")
    rev_3y = _kmv(raw, "growth", "growthRatePercentRevenue3Year")
    eps_ttm = _kmv(raw, "growth", "ePSChangePercentTTMOverTTM")
    eps_5y = _kmv(raw, "growth", "ePSGrowthRate5Year")
    ebitda_5y = _kmv(raw, "growth", "earningsBeforeInterestTaxesDepreciationAmortization5YearCAGR")

    # Self-computed YoY from the annual series (trustworthy; catches base effects)
    series = _annual_series(raw)
    rev_yoy = ni_yoy = None
    if len(series) >= 2 and series[-2]["rev"] and series[-1]["rev"]:
        rev_yoy = (series[-1]["rev"] - series[-2]["rev"]) / abs(series[-2]["rev"]) * 100
    if len(series) >= 2 and series[-2]["ni"] not in (None, 0) and series[-1]["ni"] is not None:
        ni_yoy = (series[-1]["ni"] - series[-2]["ni"]) / abs(series[-2]["ni"]) * 100

    # PROFIT-TO-REVENUE ANCHOR (graduated band + multi-year guard). Profit that grows
    # wildly faster than revenue is a margin-recovery / low-base artefact, not durable
    # business growth. Validated on real data — KIRIINDUS rev +13% but profit +2,003%,
    # INDORAMA rev +15% but profit +10,629%. Two independent checks, both must pass:
    #
    #   (A) Graduated taper on the profit/revenue-growth ratio:
    #       • profit ≤ 3× revenue growth → counted FULLY (normal operating leverage)
    #       • 3× … 10× revenue growth    → excess above 3× tapered, marginal credit
    #                                       falling linearly from full (at 3×) to 0 (at 10×)
    #       • profit ≥ 10× revenue growth→ pinned at the value the taper reaches at 10×
    #       Smooth, no cliff: a stock at 4× keeps most credit, at 9× little, at 100× capped.
    #
    #   (B) Sustained-growth guard: the ratio alone can't see that KIRIINDUS's 5-year
    #       revenue is −3% (a SHRINKING business with a one-off profit spike). So if
    #       5-year revenue growth is negative/near-zero, profit-growth credit is capped
    #       hard — a single good year on a multi-year decline is not real growth.
    ANCHOR_LOW  = 3.0    # full credit up to this multiple of revenue growth
    ANCHOR_HIGH = 10.0   # taper runs from LOW to HIGH; pinned beyond

    def _taper(profit_growth, rev_growth):
        """(A) graduated profit/revenue-growth taper. Returns credited growth %."""
        if profit_growth is None:
            return None
        if rev_growth is None or rev_growth <= 0:
            return profit_growth              # can't anchor; absolute clamp handles it
        low  = ANCHOR_LOW  * rev_growth
        high = ANCHOR_HIGH * rev_growth
        if profit_growth <= low:
            return profit_growth              # full credit (normal operating leverage)
        p = min(profit_growth, high)          # beyond HIGH contributes nothing extra
        frac = (p - low) / (high - low)       # 0 at LOW, 1 at HIGH
        # marginal credit falls linearly 1→0 across the band ⇒ integral = frac - frac²/2
        credited_excess = (frac - frac * frac / 2.0) * (high - low)
        return low + credited_excess

    def anchor_to_revenue(profit_growth, rev_growth, rev_5yr):
        """(A) taper, then (B) sustained-growth guard."""
        val = _taper(profit_growth, rev_growth)
        if val is None:
            return None
        # (B) if the business is shrinking over 5 years, a one-year profit spike is not
        # durable growth — cap the credited profit growth at a low, non-rewarding level.
        if rev_5yr is not None and rev_5yr < 3.0 and val is not None:
            val = min(val, max(rev_5yr, 0.0) + 5.0)   # ~flat: no growth reward
        return val

    # Anchor the profit-side inputs to the best available revenue-growth reference.
    rev_ref_ttm = rev_yoy if rev_yoy is not None else rev_ttm     # 1-yr reference
    eps_ttm_anchored = anchor_to_revenue(eps_ttm, rev_ref_ttm, rev_5y)
    ni_yoy_anchored  = anchor_to_revenue(ni_yoy, rev_ref_ttm, rev_5y)
    eps_5y_anchored  = anchor_to_revenue(eps_5y, rev_5y, rev_5y)  # 5-yr vs 5-yr

    # Clamp every growth input to a believable ceiling (150%) BEFORE scoring, so any
    # base-effect explosion that slipped past the anchor (e.g. no revenue to anchor
    # against) still can't max the category. 150% preserves genuine fast growers
    # (validated: real gems sit under ~150% YoY) while killing 500%+ artefacts.
    parts = [
        _scale(_clamp_growth_input(rev_ttm, 150), 0, 30),
        _scale(_clamp_growth_input(rev_5y, 150), 0, 25),
        _scale(_clamp_growth_input(rev_3y, 150), 0, 25),
        _scale(_clamp_growth_input(eps_ttm_anchored, 150), 0, 35),
        _scale(_clamp_growth_input(eps_5y_anchored, 150), 0, 25),
        _scale(_clamp_growth_input(ebitda_5y, 150), 0, 25),
        _scale(_clamp_growth_input(rev_yoy, 150), 0, 30),
    ]
    # Note: the profit YoY (ni_yoy) is used by the distress gate, not scored directly
    # here, but we anchor it so the gate sees the realistic figure too.
    s = _avg(parts)
    return s, {"rev_growth_ttm": rev_ttm, "rev_growth_5y": rev_5y, "eps_growth_ttm": eps_ttm,
               "eps_growth_5y": eps_5y, "ebitda_cagr_5y": ebitda_5y,
               "rev_yoy_calc": rev_yoy, "profit_yoy_calc": ni_yoy,
               "profit_yoy_anchored": ni_yoy_anchored}


def score_financial_strength(raw: dict) -> tuple:
    de = _kmv(raw, "financialstrength", "totalDebtPerTotalEquityMostRecentQuarter",
              "totalDebtPerTotalEquityMostRecentFiscalYear")
    ltde = _kmv(raw, "financialstrength", "lTDebtPerEquityMostRecentQuarter",
                "ltDebtPerEquityMostRecentFiscalYear")
    cur = _kmv(raw, "financialstrength", "currentRatioMostRecentQuarter", "currentRatioMostRecentFiscalYear")
    quick = _kmv(raw, "financialstrength", "quickRatioMostRecentQuarter", "quickRatioMostRecentFiscalYear")
    icov = _kmv(raw, "financialstrength", "netInterestCoverageTrailing12Month",
                "netInterestCoverageMostRecentFiscalYear")

    # Debt/equity scoring. A NEGATIVE D/E means negative equity (accumulated losses
    # have wiped out shareholder funds) — that's an insolvency red flag, NOT strength.
    # So negative D/E scores 0, not max. Only a non-negative, low D/E is good.
    def debt_score(x, cap):
        if x is None:
            return None
        if x < 0:
            return 0.0                      # negative equity = worst, not best
        return _scale(cap - x, 0, cap)      # 0 debt → 100, cap+ debt → 0

    parts = [
        debt_score(de, 2.0),
        debt_score(ltde, 1.5),
        _scale(cur, 0.8, 2.5),
        _scale(quick, 0.5, 1.5),
        _scale(icov, 1, 15) if (icov is None or icov >= 0) else 0.0,  # negative coverage = losses
    ]
    s = _avg(parts)
    return s, {"debt_to_equity": de, "lt_debt_to_equity": ltde, "current_ratio": cur,
               "quick_ratio": quick, "interest_coverage": icov}


def score_cash_quality(raw: dict) -> tuple:
    fcf = _kmv(raw, "financialstrength", "freeCashFlowtrailing12Month", "freeCashFlowMostRecentFiscalYear")
    fcf_rev = _kmv(raw, "margins", "freeOperatingCashFlowPerRevenueTTM", "freeOperatingCashFlowPerRevenue5YearAverage")

    # Accruals check: operating cash flow vs net income (cash-backed earnings = quality)
    series = _annual_series(raw)
    ocf_ni = None
    if series:
        latest = series[-1]
        if latest["ocf"] is not None and latest["ni"] not in (None, 0):
            ocf_ni = latest["ocf"] / latest["ni"]    # >1 = earnings backed by cash

    parts = [
        _scale(fcf_rev, 0, 15),                          # FCF/revenue %
        100.0 if (fcf is not None and fcf > 0) else (0.0 if fcf is not None else None),
        _scale(ocf_ni, 0.5, 1.5) if ocf_ni is not None else None,
    ]
    # 3 expected inputs; if FCF (the key signal) or others are missing, the score is
    # haircut rather than awarded full marks on partial data. This is the MOBILISE fix.
    s = _avg_complete(parts, expected=3, floor=0.5)
    return s, {"fcf": fcf, "fcf_per_revenue": fcf_rev, "ocf_to_net_income": ocf_ni}


def score_valuation(raw: dict) -> tuple:
    pe = _kmv(raw, "valuation", "pPerEBasicExcludingExtraordinaryItemsTTM",
              "pPerEIncludingExtraordinaryItemsTTM", "pPerENormalizedMostRecentFiscalYear")
    pb = _kmv(raw, "valuation", "priceToBookMostRecentQuarter", "priceToBookMostRecentFiscalYear")
    ps = _kmv(raw, "valuation", "priceToSalesTrailing12Month", "priceToSalesMostRecentFiscalYear")
    peg = _kmv(raw, "valuation", "pegRatio")
    p_fcf = _kmv(raw, "valuation", "priceToFreeCashFlowPerShareTrailing12Months")
    div_yld = _kmv(raw, "valuation", "currentDividendYieldCommonStockPrimaryIssueLTM")

    # cheaper = higher score (inverted scales)
    parts = [
        _scale(40 - pe, 0, 35) if (pe is not None and pe > 0) else (0.0 if pe is not None else None),
        _scale(8 - pb, 0, 7) if (pb is not None and pb > 0) else None,
        _scale(8 - ps, 0, 7) if (ps is not None and ps > 0) else None,
        _scale(3 - peg, 0, 2.5) if (peg is not None and peg > 0) else None,
        _scale(40 - p_fcf, 0, 35) if (p_fcf is not None and p_fcf > 0) else None,
        _scale(div_yld, 0, 4),
    ]
    s = _avg(parts)
    return s, {"pe": pe, "pb": pb, "ps": ps, "peg": peg, "price_to_fcf": p_fcf, "dividend_yield": div_yld}


def score_mgmt_ownership(raw: dict) -> tuple:
    # Promoter holding LEVEL and TREND from shareholding[]
    promoter_latest = promoter_trend = None
    for grp in (raw.get("shareholding") or []):
        if not isinstance(grp, dict):
            continue
        nm = (grp.get("displayName") or grp.get("categoryName") or "").lower()
        if "promoter" in nm:
            cats = [c for c in (grp.get("categories") or []) if isinstance(c, dict)]
            cats.sort(key=lambda c: str(c.get("holdingDate", "")))
            if cats:
                promoter_latest = _num(cats[-1].get("percentage"))
                if len(cats) >= 2:
                    first = _num(cats[0].get("percentage"))
                    if first is not None and promoter_latest is not None:
                        promoter_trend = promoter_latest - first   # +ve = increasing stake
            break

    payout = _kmv(raw, "financialstrength", "payoutRatioTrailing12Month", "payoutRatioMostRecentFiscalYear")
    asset_turn = _kmv(raw, "mgmtEffectiveness", "assetTurnoverTrailing12Month")
    inv_turn = _kmv(raw, "mgmtEffectiveness", "inventoryTurnoverTrailing12Month")
    recv_turn = _kmv(raw, "mgmtEffectiveness", "receivablesTurnoverTrailing12Month")

    parts = [
        _scale(promoter_latest, 30, 75) if promoter_latest is not None else None,
        # trend: -5%→0, +2%→100 (rising promoter stake is a positive signal)
        _scale(promoter_trend, -5, 2) if promoter_trend is not None else None,
        _scale(payout, 0, 40) if payout is not None else None,   # some payout = discipline
        _scale(asset_turn, 0.3, 1.5),
        _scale(inv_turn, 2, 10),
    ]
    s = _avg(parts)
    return s, {"promoter_holding": promoter_latest, "promoter_trend": promoter_trend,
               "payout_ratio": payout, "asset_turnover": asset_turn,
               "inventory_turnover": inv_turn, "receivables_turnover": recv_turn}


# ───────────────────────── combiner ─────────────────────────

# Buffett-quality tilt: profitability + cash quality + strength outweigh valuation.
CATEGORY_WEIGHTS = {
    "profitability":       0.25,
    "cash_quality":        0.20,
    "financial_strength":  0.18,
    "growth":              0.17,
    "valuation":           0.12,
    "mgmt_ownership":      0.08,
}


def _clamp_growth_input(x, ceiling=150.0):
    """
    Cap a growth % so base-effect explosions can't inflate the growth score.

    A stock coming off a near-zero base can post 'growth' of tens of thousands of
    percent (e.g. profit 108,336% when last year's profit was ~zero). That number
    reflects a tiny denominator, not a better business, so above a believable
    ceiling we treat all growth identically: a genuine 100% grower and a 108,336%
    base-effect artefact both get ceiling credit, no more. Negative growth passes
    through unchanged (real contraction should score low).
    """
    if x is None:
        return None
    if x > ceiling:
        return ceiling
    return x


def _distress_gate(raw: dict, category_details: dict) -> tuple:
    """
    MULTI-SIGNAL quality gate. Rather than a single hard rule ("if ROE<0 cap the
    score"), we count how many INDEPENDENT red flags a stock trips and scale the
    penalty to the weight of evidence. One flag can be noise or a one-off; several
    flags agreeing is a genuinely distressed business that shouldn't sit mid-table.

    Returns (multiplier 0..1, list_of_flags). The multiplier is applied to the final
    blended score. Evidence is drawn from DIFFERENT parts of the data so no single
    distorted field can dominate — and so no single clean field can rescue a stock
    that's broken everywhere else.

    Flags (each independent, each a real sign of distress):
      • negative_roe        — losing money on equity (profitability detail)
      • negative_equity     — D/E negative ⇒ accumulated losses wiped out net worth
      • extreme_leverage    — D/E very high (solvency risk)
      • negative_margin     — operating OR net margin below zero (core ops unprofitable)
      • weak_interest_cover — can't comfortably cover interest (< 1x)
      • negative_growth_both— BOTH revenue and profit shrinking (not a one-off)

    Penalty ladder (by count of flags):
      0 flags → 1.00 (no penalty)
      1 flag  → 0.85 (mild — could be a one-off; light haircut)
      2 flags → 0.60
      3 flags → 0.40
      4+ flags→ 0.25 (severely distressed; forced to the bottom band)
    Plus an absolute backstop: deeply negative ROE (< -20%) alone caps at 0.45,
    because a business losing a fifth of its equity is distressed regardless.
    """
    prof = category_details.get("profitability", {})
    strg = category_details.get("financial_strength", {})
    grow = category_details.get("growth", {})

    roe = prof.get("roe")
    op_margin = prof.get("op_margin")
    net_margin = prof.get("net_margin")
    de = strg.get("debt_to_equity")
    icov = strg.get("interest_coverage")
    rev_g = grow.get("rev_yoy_calc")
    prof_g = grow.get("profit_yoy_calc")

    flags = []
    if roe is not None and roe < 0:
        flags.append("negative_roe")
    if de is not None and de < 0:
        flags.append("negative_equity")
    if de is not None and de > 5:
        flags.append("extreme_leverage")
    if (op_margin is not None and op_margin < 0) or (net_margin is not None and net_margin < 0):
        flags.append("negative_margin")
    if icov is not None and icov < 1:
        flags.append("weak_interest_cover")
    if (rev_g is not None and rev_g < 0) and (prof_g is not None and prof_g < 0):
        flags.append("negative_growth_both")

    n = len(flags)
    ladder = {0: 1.00, 1: 0.85, 2: 0.60, 3: 0.40}
    mult = ladder.get(n, 0.25)   # 4+ flags → 0.25

    # Absolute backstop: catastrophic ROE alone forces a low ceiling even if, say,
    # valuation or a base-effect growth number is propping the stock up.
    if roe is not None and roe < -20 and mult > 0.45:
        mult = 0.45

    return mult, flags


def _linkage_check(raw: dict) -> tuple:
    """
    MULTI-YEAR LINKAGE CHECK (Rush's design). Links revenue, profit, equity and debt
    ACROSS a 3-5 year window and asks whether they tell a consistent story. A genuine
    business shows these moving together sensibly; an artifact shows them contradicting.

    Runs only where ≥3 annual years of data exist (shortlisted stocks are already ≥₹100
    Cr revenue, so shells/young-IPO ambiguity doesn't arise here). Compares first→last
    over the window (prefers 5 years, accepts 3).

    THREE independent linkage tests (each a real sign of incoherent fundamentals):
      T1  profit_vs_revenue — profit grew wildly out of proportion to revenue over the
          WHOLE window (not one year) while revenue barely moved ⇒ non-organic profit.
      T2  profit_vs_equity  — profit "grew" strongly but the equity base is flat/shrinking
          ⇒ earnings aren't compounding into real accumulated value.
      T3  debt_fueled       — revenue/profit growing but ONLY alongside a sharp rise in
          debt/equity ⇒ leveraged growth, not organic quality.

    Penalty is applied ONLY when 2+ tests fail (a single fail can be a legitimate one-off;
    two independent tests failing together is real evidence — multi-signal robustness):
      0-1 fails → 1.00 (no penalty)   2 fails → 0.80   3 fails → 0.60
    Returns (multiplier, detail dict). A discount only — never boosts a score.
    """
    series = _annual_series(raw)
    # A year counts as REAL operating history if it has positive revenue. That's the
    # core signal the company was operating that year. Missing equity in an OLD year is
    # common (the data source often omits old balance sheets) and must NOT erase an
    # otherwise-real year — so equity absence alone doesn't disqualify. What DOES
    # disqualify is NEGATIVE equity (a shell / post-insolvency year, e.g. WAAREEINDO
    # /Indosolar: rev=0, equity=-990 until FY25) or zero/None revenue.
    def _real(r):
        rev, eq = r.get("rev"), r.get("equity")
        if rev is None or rev <= 0:
            return False                 # not operating that year
        if eq is not None and eq < 0:
            return False                 # negative-equity shell year
        return True
    real_yrs = [r for r in series if _real(r)]
    n_real = len(real_yrs)

    # HISTORY-QUALITY HAIRCUT: a company with only 1-2 real operating years has no
    # track record to trust, however good those years look. It should not be scored as
    # confidently as a 5-6 year compounder. Catches post-insolvency restarts and freshly
    # turned-around shells that slip through the coherence tests because their broken
    # early years break the growth maths (division-by-zero → None → 0 fails).
    #
    # We compare REAL operating years (positive rev, non-negative equity) against the
    # TOTAL number of annual rows the source returned. Counting total rows (not just
    # rev-not-None rows) matters because _annual_series nulls zero-revenue years — a
    # shell's dead years become rev=None, which would otherwise hide them from the
    # count and let the shell escape. Using the full row count keeps them visible.
    total_rows = len(series)
    if total_rows >= 4 and n_real <= 2:
        # ≥4 annual rows exist but ≤2 are real operating years ⇒ genuine shell/restart
        hist_ladder = {0: 0.45, 1: 0.45, 2: 0.60}
        hist_mult = hist_ladder.get(n_real, 1.00)
    elif total_rows >= 4 and n_real == 3:
        hist_mult = 0.85
    else:
        hist_mult = 1.00                 # not enough rows to judge history quality → don't haircut

    yrs = [r for r in series if r.get("rev") is not None][-5:]
    if len(yrs) < 3:
        base_detail = {"linkage": "skipped_lt3yr", "years": len(yrs),
                       "real_years": n_real, "history_mult": round(hist_mult, 3),
                       "one_off_spike": False, "spike_mult": 1.00,
                       "linkage_only_mult": 1.00}
        return round(hist_mult, 3), base_detail

    first, last = yrs[0], yrs[-1]

    def g(a, b):
        if a is None or b is None or a == 0:
            return None
        return (b - a) / abs(a)

    rev_g = g(first.get("rev"), last.get("rev"))
    ni_g  = g(first.get("ni"),  last.get("ni"))
    eq_g  = g(first.get("equity"), last.get("equity"))

    # debt/equity ratio at first vs last (trend of leverage)
    def de_ratio(r):
        d, e = r.get("debt"), r.get("equity")
        if d is None or e is None or e == 0:
            return None
        return d / e
    de_first, de_last = de_ratio(first), de_ratio(last)

    fails = []

    # T1: profit grew >3× revenue growth over the window AND revenue barely moved
    if rev_g is not None and ni_g is not None:
        if ni_g > 1.0 and rev_g < 0.20:                     # profit >+100% but revenue <+20% over 3-5yr
            fails.append("T1_profit_vs_revenue")
        elif rev_g > 0 and ni_g > 3.0 * rev_g and ni_g > 0.5:  # profit >3× revenue growth, meaningful
            fails.append("T1_profit_vs_revenue")

    # T2: profit up strongly but equity base flat/shrinking
    if ni_g is not None and eq_g is not None:
        if ni_g > 0.5 and eq_g <= 0.0:
            fails.append("T2_profit_vs_equity")

    # T3: leverage rose sharply over the window (debt-fuelled growth)
    if de_first is not None and de_last is not None:
        if de_last > de_first + 0.5 and de_last > 1.0:      # D/E climbed >0.5 and is now elevated
            fails.append("T3_debt_fueled")

    # ONE-OFF PROFIT SPIKE (strong single signal): a final-year net income that
    # explodes vs the prior year while revenue barely moves is almost always a
    # non-operating one-off (asset sale, revaluation, exceptional item) — not
    # earnings power. KIRIINDUS ni 265→5,566 on flat revenue; JSWDULUX 430→1,974.
    # This is decisive enough to penalise on its own, unlike the window tests.
    spike = False
    if len(yrs) >= 2:
        prev, curr = yrs[-2], yrs[-1]
        pn, cn = prev.get("ni"), curr.get("ni")
        pr, cr = prev.get("rev"), curr.get("rev")
        if pn is not None and cn is not None and pn > 0 and cn > 0 and cn > 4.0 * pn:
            # profit >4× last year — check revenue didn't remotely keep pace.
            # (4× not 5×: JSWDULUX ni 430→1,974 is 4.6× on flat revenue — a clear
            # one-off that a 5× cutoff missed. No organic business quadruples annual
            # profit on flat revenue, so 4× is still safe against real growers.)
            rev_ratio = (cr / pr) if (pr and cr and pr > 0) else None
            if rev_ratio is None or rev_ratio < 1.5:        # revenue grew <50% while profit >400%
                spike = True

    n = len(fails)
    ladder = {0: 1.00, 1: 1.00, 2: 0.80, 3: 0.60}           # penalise only at 2+
    linkage_mult = ladder.get(n, 0.60)
    spike_mult = 0.60 if spike else 1.00                    # one-off spike ⇒ ~40% haircut

    # Combine the three independent discounts (history quality, window coherence,
    # one-off spike). Take the STRONGEST (lowest) — they measure different failures
    # and we don't want to double-count, but any one being severe should dominate.
    mult = min(hist_mult, linkage_mult, spike_mult)

    return round(mult, 3), {
        "linkage_fails": fails,
        "n_fails": n,
        "real_years": n_real,
        "history_mult": round(hist_mult, 3),
        "one_off_spike": spike,
        "spike_mult": spike_mult,
        "linkage_only_mult": linkage_mult,
        "rev_growth_window":    None if rev_g is None else round(rev_g, 3),
        "profit_growth_window": None if ni_g is None else round(ni_g, 3),
        "equity_growth_window": None if eq_g is None else round(eq_g, 3),
        "de_first": None if de_first is None else round(de_first, 3),
        "de_last":  None if de_last is None else round(de_last, 3),
        "years": len(yrs),
    }


def _is_financial(raw: dict) -> tuple:
    """
    Detect banks / NBFCs / AMCs / insurers. For these, FCF and current-ratio are
    MEANINGLESS and high debt/equity is NORMAL (deposits & borrowings are their raw
    material, not distress). Scoring them with manufacturing-company logic wrongly
    tanks cash_quality & financial_strength and mis-fires the leverage distress flag —
    which is why strong banks (SBIN, HDFCBANK, RECLTD, PFC, Bajaj Finance) ranked far
    too low. This flags them so the combiner can use a sector-appropriate lens.

    Multi-signal detection (robust to missing fields — no single string relied on):
      1. industry / sector string mentions a financial business, OR
      2. the company name matches a clear financial pattern (bank/finance/capital/…), OR
      3. structural fingerprint: interest income is a large share of revenue AND there
         is no meaningful FCF line (financials don't report FCF the way industrials do).
    Returns (is_financial: bool, reason: str).
    """
    # --- (1) industry / sector text ---
    text_fields = []
    for key in ("industry", "mgIndustry", "sector", "sectorName", "industryName"):
        v = raw.get(key)
        if isinstance(v, str):
            text_fields.append(v)
    cp = raw.get("companyProfile") or {}
    if isinstance(cp, dict):
        for key in ("industry", "sector", "mgIndustry", "industryName"):
            v = cp.get(key)
            if isinstance(v, str):
                text_fields.append(v)
    blob = " ".join(text_fields).lower()
    FIN_WORDS = ("bank", "financ", "nbfc", "lending", "capital market", "broker",
                 "asset management", "amc", "insurance", "insurer", "housing finance",
                 "microfinance", "wealth", "securities", "investment")
    if any(w in blob for w in FIN_WORDS):
        return True, f"industry:{blob[:40]}"

    # --- (2) name pattern (fallback when industry text is absent) ---
    name = str(raw.get("companyName") or raw.get("name") or "").lower()
    NAME_WORDS = (" bank", "finance", "financial", "capital", "fincorp", "finserv",
                  "housing finance", "asset management", "insurance", "securities",
                  "wealth", "broking", "nbfc", "microfin", "credit")
    if any(w in name for w in NAME_WORDS):
        # guard: don't catch e.g. "Finolex" / "Financial Technologies"-style false hits
        # by requiring the word as a token-ish match already handled by spaces above.
        return True, f"name:{name[:40]}"

    # --- (3) structural fingerprint ---
    series = _annual_series(raw)
    if series:
        last = series[-1]
        rev = last.get("rev")
        # interest income share (if present in INC as a separate key handled elsewhere)
        # here we use the absence of a real FCF/OCF line + presence of large debt as a
        # weak signal; kept conservative to avoid false positives on capital-heavy
        # industrials, so this branch only triggers with corroboration.
        # (Intentionally minimal — text/name catch the vast majority.)
    return False, ""


# ── DATA-INTEGRITY CHECK ──────────────────────────────────────────────────────
# Ported from the US engine, adapted to NSE's data shape. It penalises data that
# CONTRADICTS ITSELF, which is evidence something is wrong with the numbers.
#
# Deliberately distinct from the coverage penalty above:
#   * MISSING data  -> handled by coverage (you didn't earn the points)
#   * CONTRADICTORY data -> handled here (the numbers disagree with each other)
# Conflating the two would punish a thinly-covered small cap as if its filings
# were suspect, which isn't fair or useful.
#
# The multiplier is FLOORED so data hygiene can never dominate the score - the
# blend should still mostly reflect business quality.
_INTEGRITY_FLOOR = 0.80          # worst case: score scaled to 80%
# Growth above which a figure is almost certainly a base effect rather than
# performance. SCALE-AWARE: 300% is the right bar for a large company, but on a
# small revenue base a near-zero prior year makes triple-digit growth trivial
# arithmetic. A Rs 170cr company "growing" 126% is not the same evidence as a
# Rs 5,000cr company doing it.
_IMPLAUSIBLE_GROWTH = 300.0                  # default / large-cap bar
_SMALL_REVENUE_CR = 500.0                    # below this, apply the tighter bar
_IMPLAUSIBLE_GROWTH_SMALL = 100.0            # tighter bar for small-revenue cos


def _integrity_check(raw: dict, category_details: dict) -> tuple:
    """Return (multiplier, [flags]). Multiplier is _INTEGRITY_FLOOR..1.0."""
    flags = []

    series = _annual_series(raw)

    # 1) Revenue growth implausible for the company's SIZE. A near-zero prior
    #    year producing a huge percentage is arithmetic, not performance - and
    #    how huge it needs to be before that's true depends on scale.
    latest_rev = None
    if series:
        latest_rev = _num(series[-1].get("rev"))

    growth_pct = None
    if len(series) >= 2:
        prev, cur = series[-2], series[-1]
        pr, cr = prev.get("rev"), cur.get("rev")
        if pr and cr and pr > 0:
            growth_pct = (cr - pr) / pr * 100
            bar = (_IMPLAUSIBLE_GROWTH_SMALL
                   if (latest_rev is not None and latest_rev < _SMALL_REVENUE_CR)
                   else _IMPLAUSIBLE_GROWTH)
            if growth_pct > bar:
                scale = "small-cap " if bar == _IMPLAUSIBLE_GROWTH_SMALL else ""
                flags.append(f"implausible {scale}revenue growth {growth_pct:,.0f}% "
                             f"on Rs {latest_rev:,.0f}cr base (base effect)")

    # 1b) HIGH REPORTED GROWTH + NEGATIVE FREE CASH FLOW.
    #     This pairing is the classic signature of low-quality earnings: profit
    #     appears on the income statement while the business consumes cash. The
    #     engine scores growth and cash quality in SEPARATE categories, so a
    #     company can score ~100 on growth and 0 on cash and still finish high
    #     because the other four categories carry it - the contradiction between
    #     the two is never noticed. Cash is far harder to manipulate than
    #     accounting profit, so when they disagree, that disagreement is itself
    #     the signal.
    cash_det = category_details.get("cash_quality", {}) or {}
    fcf = _num(cash_det.get("fcf"))
    if growth_pct is not None and fcf is not None and fcf < 0 and growth_pct > 40:
        flags.append(f"growth {growth_pct:,.0f}% but free cash flow is negative "
                     f"(Rs {fcf:,.1f}cr) - earnings not backed by cash")

    # 2) Profit exceeding revenue - impossible from operations. Usually a one-off
    #    (asset sale, revaluation) being read as operating performance.
    if series:
        cur = series[-1]
        rev, ni = cur.get("rev"), cur.get("ni")
        if rev and ni and rev > 0 and ni > rev:
            flags.append(f"net income (Rs {ni:,.0f}cr) exceeds revenue (Rs {rev:,.0f}cr)")

    # 3) ROE that the raw inputs can't support. The engine already dampens
    #    uncorroborated ROE; this flags the extreme residual cases.
    prof = category_details.get("profitability", {}) or {}
    roe = _num(prof.get("roe"))
    if roe is not None and roe > 200:
        flags.append(f"ROE {roe:,.0f}% implausible even after corroboration")

    # 4) Equity going negative while the company is scored as healthy - the two
    #    statements can't both be true.
    if series:
        eq = series[-1].get("equity")
        if eq is not None and eq < 0:
            fs = category_details.get("financial_strength", {}) or {}
            if _num(fs.get("score")) is not None and _num(fs.get("score")) > 50:
                flags.append("negative equity but financial strength scored healthy")

    # 5) Revenue collapse that contradicts a positive growth score.
    if len(series) >= 2:
        prev, cur = series[-2], series[-1]
        pr, cr = prev.get("rev"), cur.get("rev")
        if pr and cr and pr > 0:
            g = (cr - pr) / pr * 100
            gs = category_scores_growth = category_details.get("growth", {}) or {}
            gscore = _num(gs.get("score"))
            if g < -60 and gscore is not None and gscore > 55:
                flags.append(f"revenue fell {g:,.0f}% but growth scored {gscore:.0f}")

    if not flags:
        return 1.0, []

    # Ladder: more independent contradictions -> stronger penalty, floored.
    ladder = {1: 0.94, 2: 0.88, 3: 0.83}
    mult = ladder.get(len(flags), _INTEGRITY_FLOOR)
    return max(mult, _INTEGRITY_FLOOR), flags


def compute_fundamental_score(raw: dict) -> dict:
    """
    Run all six category scorers over a raw IndianAPI response and return a full
    breakdown: per-category scores, the details behind each, and the weighted total.
    Pure function over stored raw data — safe to re-run for the whole universe.
    """
    cats = {
        "profitability":      score_profitability(raw),
        "growth":             score_growth(raw),
        "financial_strength": score_financial_strength(raw),
        "cash_quality":       score_cash_quality(raw),
        "valuation":          score_valuation(raw),
        "mgmt_ownership":     score_mgmt_ownership(raw),
    }

    category_scores = {k: (v[0] if v[0] is not None else None) for k, v in cats.items()}
    category_details = {k: v[1] for k, v in cats.items()}

    # FINANCIAL-SECTOR LENS: for banks/NBFCs/AMCs/insurers, FCF & current-ratio are
    # meaningless and high leverage is normal. Judge them on profitability, growth,
    # valuation and ownership; drop cash_quality from the blend and down-weight
    # financial_strength (its D/E & current-ratio inputs don't apply). This lifts
    # genuinely strong financials that manufacturing-logic wrongly buried.
    is_fin, fin_reason = _is_financial(raw)
    weights = dict(CATEGORY_WEIGHTS)
    if is_fin:
        weights["cash_quality"] = 0.0            # FCF meaningless for lenders
        weights["financial_strength"] = 0.06     # down-weighted (D/E normal-high)
        # redistribute the freed weight to profitability & growth (the real signals)
        weights["profitability"] = CATEGORY_WEIGHTS["profitability"] + 0.14
        weights["growth"] = CATEGORY_WEIGHTS["growth"] + 0.06

    # Weighted blend over categories that have a score; renormalise weights so a
    # missing (or zero-weighted) category doesn't unfairly drag the total.
    num = den = 0.0
    for k, w in weights.items():
        sc = category_scores.get(k)
        if sc is not None and w > 0:
            num += sc * w
            den += w
    total = num / den if den > 0 else 0.0

    # COVERAGE PENALTY
    # --------------------------------------------------------------------------
    # Renormalising above is right in spirit - one missing category shouldn't
    # tank an otherwise good company - but on its own it makes MISSING DATA AN
    # ADVANTAGE: dropping a category from BOTH numerator and denominator means a
    # stock measured on 1 of 6 categories gets that single category's score as
    # its ENTIRE total. In practice that put stocks with blank ROE, D/E and
    # growth at the very top of the universe, above companies measured on all
    # six. A 90 earned from 10% of the weight is not evidence of quality; it's
    # evidence we couldn't measure the company.
    #
    # So the blend is scaled by how much of the weight was actually measured.
    # A floor keeps a thinly-covered stock from being zeroed outright - it just
    # sinks to where the evidence supports, rather than topping the list.
    total_weight = sum(w for w in weights.values() if w > 0)
    coverage = (den / total_weight) if total_weight > 0 else 0.0

    # A SQUARE-ROOT curve, not linear. Linear scaling overcorrected: a company
    # measured on 5 of 6 categories (coverage 0.80) lost a fifth of its score,
    # which is the very thing the original renormalisation was there to prevent.
    # sqrt is forgiving near full coverage (0.80 -> x0.89, an 11% trim) and steep
    # when most of the picture is missing (0.20 -> x0.45). The floor stops a
    # thinly-covered stock being zeroed outright - it just sinks to where the
    # evidence supports rather than topping the list.
    COVERAGE_FLOOR = 0.35
    coverage_mult = max(coverage ** 0.5, COVERAGE_FLOOR) if coverage > 0 else 0.0
    total = total * coverage_mult

    # MULTI-SIGNAL distress gate: scale the blended score by weight-of-evidence.
    # For financials, suppress the leverage-based flags (high D/E is their normal).
    distress_mult, distress_flags = _distress_gate(raw, category_details)
    if is_fin and distress_flags:
        distress_flags = [f for f in distress_flags
                          if f not in ("extreme_leverage", "negative_equity")]
        # recompute multiplier from the reduced flag set (same ladder as the gate)
        n = len(distress_flags)
        ladder = {0: 1.00, 1: 0.85, 2: 0.60, 3: 0.40}
        distress_mult = ladder.get(n, 0.25)
        roe_v = category_details.get("profitability", {}).get("roe")
        if roe_v is not None and roe_v < -20 and distress_mult > 0.45:
            distress_mult = 0.45

    # MULTI-YEAR LINKAGE check: link revenue/profit/equity/debt across 3-5yr; penalise
    # only when 2+ independent linkage tests fail (a single fail can be a one-off).
    linkage_mult, linkage_detail = _linkage_check(raw)

    # Data-integrity: penalise numbers that contradict each other (distinct from
    # the coverage penalty, which handles numbers that are simply absent).
    integrity_mult, integrity_flags = _integrity_check(raw, category_details)

    total = round(total * distress_mult * linkage_mult * integrity_mult, 1)

    return {
        "score": total,
        "coverage": round(coverage, 3),
        "coverage_multiplier": round(coverage_mult, 3),
        "integrity_multiplier": round(integrity_mult, 3),
        "integrity_flags": integrity_flags,
        "category_scores": {k: (round(v, 1) if v is not None else None)
                            for k, v in category_scores.items()},
        "category_details": category_details,
        "weights": weights,
        "is_financial": is_fin,
        "financial_reason": fin_reason,
        "distress_multiplier": distress_mult,
        "distress_flags": distress_flags,
        "linkage_multiplier": linkage_mult,
        "linkage_detail": linkage_detail,
    }
