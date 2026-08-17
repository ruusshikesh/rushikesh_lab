"""
Rush Algo - US Fundamentals (Finnhub, 6 endpoints per stock)
=============================================================
Mirrors data/fundamental.py's safety design (incremental per-symbol cache,
atomic writes, skip-if-fresh, progress tracking) but sources from Finnhub.
Fully separate from the NSE module - shares NO code path, so a bug here cannot
touch NSE data.

ENDPOINTS USED (all verified working on the free tier against a real AAPL run):
  stock/symbol                -> US common-stock list          (1 call TOTAL)
  stock/metric                -> 133 ratios: ROE, D/E, P/E, growth, margins
  stock/profile2              -> name, industry, sharesOutstanding, float
  stock/financials-reported   -> 16 years of full BS / IS / CF
  quote                       -> live price
  stock/recommendation        -> analyst consensus
  stock/insider-transactions  -> insider buys/sells
  (stock/candle is Premium-gated - we use yfinance for price history instead)

UNITS - VERIFIED against known real AAPL values, never guessed:
  marketCapitalization / enterpriseValue -> MILLIONS USD  (4546962.5 = $4.55T)
  shareOutstanding (profile2)            -> MILLIONS      (14687.36 = 14.69B sh)
  roeTTM, margins, growth                -> ALREADY percentages (137.18 = 137%)
  D/E, P/E, P/S, current ratio           -> plain ratios
  financials-reported values             -> raw USD
  => only the documented "millions" fields are scaled. No magnitude-guessing
     heuristics: a wrong guess silently corrupts every ranking downstream.

SENTIMENT IS STORED, NOT SCORED: analyst recommendations and insider
transactions are saved to a SEPARATE US-only extras store and shown in Deep
Dive, but never feed the quality score (per design decision - mixing sentiment
into a fundamental number makes it mean two things at once).

RATE LIMIT: Finnhub free tier = 60 calls/min. The sliding-window limiter below
is set to 55/min for headroom and blocks BEFORE the window is exceeded, so a
burst can never trip the limit and get the key blocked.
  ~6 calls/stock x ~4000 stocks = ~24,000 calls = ~7.3 hours for a full run.
  Fully resumable: each symbol is saved the instant it's fetched.

Config (.env): FINNHUB_API_KEY
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional

import requests

from config import settings
from models.schemas import FundamentalData

logger = logging.getLogger(__name__)

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_BACKEND_DIR, "data_cache_us")
CACHE_FILE = os.path.join(CACHE_DIR, "fundamental_universe_us.json")
SYMBOL_CACHE_FILE = os.path.join(CACHE_DIR, "fundamentals_by_symbol_us.json")
# US-only extras (derived metrics + sentiment). Kept OUT of FundamentalData so
# the NSE-shared schema stays untouched.
EXTRAS_CACHE_FILE = os.path.join(CACHE_DIR, "extras_by_symbol_us.json")

FINNHUB_BASE = "https://finnhub.io/api/v1"

_MILLIONS_FIELDS = {"marketCapitalization", "enterpriseValue"}

_ALIASES = {
    "roe":            ["roeTTM", "roeRfy", "roeAnnual", "roe5Y"],
    "debt_to_equity": ["totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual",
                       "longTermDebt/equityQuarterly", "longTermDebt/equityAnnual"],
    "revenue_growth": ["revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy",
                       "revenueGrowthAnnual", "revenueGrowth3Y", "revenueGrowth5Y"],
    "pe_ratio":       ["peTTM", "peAnnual", "peBasicExclExtraTTM"],
    "ps_ratio":       ["psTTM", "psAnnual"],
    "current_ratio":  ["currentRatioQuarterly", "currentRatioAnnual"],
    "profit_growth":  ["epsGrowthTTMYoy", "epsGrowthQuarterlyYoy", "epsGrowthAnnual"],
    "net_margin":     ["netProfitMarginTTM", "netProfitMarginAnnual"],
    "op_margin":      ["operatingMarginTTM", "operatingMarginAnnual"],
    "revenue_cagr":   ["revenueGrowth5Y", "revenueGrowth3Y"],
}

# XBRL-ish concept names vary between filers; try each in order.
_IC_REVENUE = ["revenue", "revenues", "totalrevenue", "netsales", "salesrevenuenet",
               "revenuefromcontractwithcustomerexcludingassessedtax"]
_IC_NETINC  = ["netincome", "netincomeloss", "profitloss", "netincomelossavailable"]
_IC_OPINC   = ["operatingincome", "operatingincomeloss"]
_CF_OCF     = ["netcashprovidedbyusedinoperatingactivities", "cashflowfromoperations",
               "netcashprovidedbyoperatingactivities", "operatingcashflow"]
_CF_CAPEX   = ["paymentstoacquirepropertyplantandequipment", "capitalexpenditure",
               "purchaseofpropertyandequipment", "capitalexpenditures"]
# Debt = actual BORROWINGS only. The earlier version included a loose
# "liabilities" fallback, which substring-matched "Total current liabilities"
# (accounts payable, deferred revenue, accruals...) - so a growing company's
# expanding payables looked like exploding debt. Verified against real AAPL
# filings: correct debt is LongTermDebtNoncurrent ($78.3B) + LongTermDebtCurrent
# ($12.3B) = $90.6B, NOT "Total current liabilities" ($165.6B).
# Long-term (non-current) portion:
_BS_DEBT_LT = ["longtermdebtnoncurrent", "longtermdebt", "longtermborrowings",
               "longtermnotespayable"]
# Current portion of borrowings (excludes payables/accruals/deferred revenue):
_BS_DEBT_ST = ["longtermdebtcurrent", "shorttermborrowings", "commercialpaper",
               "notespayablecurrent", "debtcurrent"]


# ---------------------------------------------------------------------------
# Rate limiter (sliding window - verified: never exceeds the cap)
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, max_calls: int, period_sec: float):
        self.max_calls = max_calls
        self.period = period_sec
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.time()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0]) + 0.05
            time.sleep(max(wait, 0.05))


_limiter = _RateLimiter(max_calls=55, period_sec=60.0)   # headroom under 60/min


# ---------------------------------------------------------------------------
# Progress (polled by the frontend)
# ---------------------------------------------------------------------------
_progress = {
    "running": False, "total": 0, "done": 0, "fetched": 0, "failed": 0,
    "current": "", "started_at": None, "finished_at": None, "message": "idle",
    "stopping": False,
}

# Cooperative stop flag. The fetch loop checks this between symbols and exits
# cleanly - it finishes and SAVES the symbol currently in flight first, so a
# stop never loses work. Resuming is just triggering a refresh again: already
# cached symbols are skipped, so it continues where it left off.
_stop_requested = threading.Event()


def request_stop() -> dict:
    """Ask a running fetch to stop after the current symbol completes."""
    if _progress.get("running"):
        _stop_requested.set()
        _progress["stopping"] = True
        _progress["message"] = "stopping after current symbol..."
    return get_progress()


def clear_stop() -> None:
    _stop_requested.clear()
    _progress["stopping"] = False


def get_progress() -> dict:
    p = dict(_progress)
    p["pct"] = round(p["done"] / p["total"] * 100, 1) if p["total"] else 0.0
    return p


# ---------------------------------------------------------------------------
# Cache helpers (crash-safe, same pattern as the NSE module)
# ---------------------------------------------------------------------------
def _ensure_dirs():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _atomic_write_json(path: str, data) -> None:
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json(path: str) -> dict:
    _ensure_dirs()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("US cache read failed (%s): %s - starting fresh", path, exc)
        return {}


def _load_symbol_cache() -> dict:
    return _load_json(SYMBOL_CACHE_FILE)


def _save_symbol_cache(cache: dict) -> None:
    _atomic_write_json(SYMBOL_CACHE_FILE, cache)


def load_extras() -> dict:
    return _load_json(EXTRAS_CACHE_FILE)


def get_extras(symbol: str) -> dict:
    return load_extras().get(symbol.upper(), {})


# ---------------------------------------------------------------------------
# Finnhub API
# ---------------------------------------------------------------------------
def _api_key() -> str:
    key = getattr(settings, "FINNHUB_API_KEY", "") or os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        raise RuntimeError("FINNHUB_API_KEY not set (config.py / .env / environment).")
    return key


def _finnhub_get(path: str, params: dict, retries: int = 3):
    """Rate-limited GET. Returns parsed JSON or None - never raises (except on a
    bad API key), so one bad symbol can't kill a multi-hour batch run."""
    params = {**params, "token": _api_key()}
    url = f"{FINNHUB_BASE}/{path}"
    for attempt in range(retries):
        _limiter.acquire()
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 429:
                logger.warning("Finnhub 429 on %s - backing off %ds", path, 10 * (attempt + 1))
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("Finnhub rejected the API key (401). Check FINNHUB_API_KEY.")
            if r.status_code == 403:
                logger.debug("Finnhub %s is premium-gated (403)", path)
                return None
            if r.status_code != 200:
                return None
            return r.json()
        except RuntimeError:
            raise
        except Exception as exc:
            logger.debug("Finnhub %s failed (%d/%d): %s", path, attempt + 1, retries, exc)
            time.sleep(1.5 * (attempt + 1))
    return None


def _pick(d: dict, keys: List[str]):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _to_usd(value, field_name: str):
    """Scale ONLY the documented millions-denominated fields. Explicit whitelist
    rather than a magnitude heuristic - guessing silently corrupts rankings."""
    v = _num(value)
    if v is None:
        return None
    return v * 1e6 if field_name in _MILLIONS_FIELDS else v


# Line items whose presence means a row is NOT the headline figure, even though
# it contains the same words. Without these, a plain substring match happily
# returns "Cost of revenue" or "Deferred revenue" as revenue, or "Net income
# attributable to noncontrolling interests" as net income - producing a
# confidently wrong number that silently corrupts margins, FCF and the score.
_EXCLUDE_WORDS = (
    "costof", "deferred", "unearned", "contractwithcustomer",
    "noncontrolling", "minorityinterest", "comprehensive", "adjustment",
    "pershare", "diluted", "basic", "percent", "ratio", "tax",
    "accumulated", "segment", "discontinued",
)


def _clean(s: str) -> str:
    return str(s or "").lower().replace(" ", "").replace(",", "").replace("_", "")


def _find_line(items: List[dict], name_options: List[str]):
    """Find a headline value in a reported-financials section.

    Matching is deliberately staged rather than 'first substring wins':
      1) reject rows containing disqualifying words (cost of / deferred /
         noncontrolling / per-share / comprehensive ...)
      2) prefer an EXACT match on the normalised concept or label
      3) only then fall back to a substring match

    Why: filers order their statements differently, so a naive first-substring
    match returns whichever similar-sounding row happens to appear first. On a
    filer that lists "Cost of revenue" above "Revenue", the old logic returned
    cost as revenue - and nothing downstream would flag it.
    """
    if not items:
        return None

    rows = []
    for it in items:
        concept_raw = _clean(it.get("concept"))
        # concepts look like "us-gaap_NetIncomeLoss" - drop the namespace
        concept = concept_raw.split("-gaap")[-1].lstrip("_") if "-gaap" in concept_raw else concept_raw
        label = _clean(it.get("label"))
        v = _num(it.get("value"))
        if v is None:
            continue
        rows.append((concept, label, v))

    def _disqualified(concept: str, label: str, want: str) -> bool:
        # Only exclude when the disqualifying word isn't part of the thing we
        # actually asked for (e.g. don't reject "IncomeTaxExpense" when the
        # caller explicitly searched for a tax line).
        for bad in _EXCLUDE_WORDS:
            if bad in want:
                continue
            if bad in concept or bad in label:
                return True
        return False

    # Pass 1: exact match on concept or label
    for want in name_options:
        for concept, label, v in rows:
            if (concept == want or label == want) and not _disqualified(concept, label, want):
                return v

    # Pass 2: substring, but still respecting the exclusions
    for want in name_options:
        for concept, label, v in rows:
            if (want in concept or want in label) and not _disqualified(concept, label, want):
                return v

    return None


# ---------------------------------------------------------------------------
# Derived multi-year metrics (the thing that makes the 6-cat engine possible)
# ---------------------------------------------------------------------------
def _total_debt(bs_items: List[dict]) -> Optional[float]:
    """Total interest-bearing debt = long-term borrowings + current portion.

    Deliberately does NOT fall back to "total liabilities": that figure bundles
    payables, accruals and deferred revenue, so it grows with the business and
    would make every expanding company look increasingly indebted. Returning
    None when no genuine borrowing line exists is better than a confidently
    wrong number - the debt-trend component simply contributes nothing instead
    of penalising the company for a metric we couldn't actually measure."""
    if not bs_items:
        return None
    lt = _find_line(bs_items, _BS_DEBT_LT)
    st = _find_line(bs_items, _BS_DEBT_ST)
    if lt is None and st is None:
        return None
    return (lt or 0.0) + (st or 0.0)


def _derive_from_financials(reports: List[dict]) -> dict:
    """Turn N years of reported statements into trend/consistency metrics.

    Returns keys consumed by fundamental_engine_us: revenue_cagr,
    revenue_growth_consistency, profitable_years_frac, years_of_history,
    fcf, fcf_margin, operating_margin, net_margin, debt_trend_pct.
    Every value is optional - a company with only 1-2 years of filings simply
    contributes less, rather than producing a fabricated number."""
    out: Dict[str, Optional[float]] = {}
    if not reports:
        return out

    # reports arrive newest-first from Finnhub; make oldest-first for trends
    rows = []
    for r in reports:
        rep = r.get("report") or {}
        rows.append({
            "year": r.get("year"),
            "revenue": _find_line(rep.get("ic"), _IC_REVENUE),
            "net_income": _find_line(rep.get("ic"), _IC_NETINC),
            "op_income": _find_line(rep.get("ic"), _IC_OPINC),
            "ocf": _find_line(rep.get("cf"), _CF_OCF),
            "capex": _find_line(rep.get("cf"), _CF_CAPEX),
            "debt": _total_debt(rep.get("bs")),
        })
    rows = [r for r in rows if r["year"]]
    rows.sort(key=lambda r: r["year"])
    if not rows:
        return out

    out["years_of_history"] = len(rows)

    # Earnings consistency: fraction of years with positive net income
    ni = [r["net_income"] for r in rows if r["net_income"] is not None]
    if ni:
        out["profitable_years_frac"] = sum(1 for x in ni if x > 0) / len(ni)

    # Revenue CAGR over a 3-5 YEAR window, with per-year plausibility checks.
    #
    # WINDOW: at least 3 years before a CAGR is trusted at all (a "5-year CAGR"
    # computed from 1 year of data is fiction - exactly what produced a -29.4%
    # figure for a company that spun out in 2024). At most 5 years, because
    # beyond that the business is often materially different - same reasoning
    # as the 5-year debt-trend window.
    #
    # PLAUSIBILITY: each year-over-year step is checked. A move above +500% or
    # below -90% is a base-effect artifact or a restatement, not performance;
    # such years are recorded so the integrity check can flag them rather than
    # silently feeding a nonsense CAGR into the score.
    CAGR_MIN_YEARS, CAGR_MAX_YEARS = 3, 5
    revs = [(r["year"], r["revenue"]) for r in rows if r["revenue"] and r["revenue"] > 0]

    suspicious_years = []
    for i in range(1, len(revs)):
        prev_v, cur_v = revs[i - 1][1], revs[i][1]
        if prev_v > 0:
            step = (cur_v - prev_v) / prev_v * 100
            if step > 500 or step < -90:
                suspicious_years.append(f"{revs[i][0]}:{step:+,.0f}%")
    if suspicious_years:
        out["suspicious_revenue_years"] = suspicious_years

    if len(revs) >= CAGR_MIN_YEARS:
        window = revs[-(CAGR_MAX_YEARS + 1):]          # up to 5 spans back
        (y0, v0), (y1, v1) = window[0], window[-1]
        span = max(1, y1 - y0)
        try:
            out["revenue_cagr"] = round(((v1 / v0) ** (1.0 / span) - 1) * 100, 2)
            out["revenue_cagr_years"] = span
        except (ValueError, ZeroDivisionError, OverflowError):
            pass
        ups = sum(1 for i in range(1, len(window)) if window[i][1] > window[i - 1][1])
        if len(window) > 1:
            out["revenue_growth_consistency"] = ups / (len(window) - 1)
    elif len(revs) >= 2:
        # Enough to measure consistency, but NOT enough for a trustworthy CAGR.
        # Leave revenue_cagr unset so the engine falls back to current YoY.
        ups = sum(1 for i in range(1, len(revs)) if revs[i][1] > revs[i - 1][1])
        out["revenue_growth_consistency"] = ups / (len(revs) - 1)

    latest = rows[-1]

    # Margins from the most recent full year
    if latest["revenue"]:
        if latest["net_income"] is not None:
            out["net_margin"] = round(latest["net_income"] / latest["revenue"] * 100, 2)
        if latest["op_income"] is not None:
            out["operating_margin"] = round(latest["op_income"] / latest["revenue"] * 100, 2)

    # Free cash flow = operating cash flow - capex (capex may be signed either way)
    if latest["ocf"] is not None:
        capex = latest["capex"]
        fcf = latest["ocf"] - abs(capex) if capex is not None else latest["ocf"]
        out["fcf"] = round(fcf, 2)
        if latest["revenue"]:
            out["fcf_margin"] = round(fcf / latest["revenue"] * 100, 2)

    # Debt trend over a ~5-YEAR window (not the full 16-year history).
    #
    # WHY 5 YEARS: comparing 2013 to 2025 says very little about a company's
    # current financial health - businesses refinance, acquire and restructure.
    # On real AAPL data the full-history read was +435%, which "correctly"
    # penalised Apple for deliberately taking on cheap debt to fund buybacks
    # while holding far more in cash. Over 5 years the same company reads
    # roughly flat-to-down, which is the fair signal. What this component is
    # actually for is catching a company rapidly levering UP right now - and
    # that only needs a recent window. Current leverage itself is already
    # measured by debt_to_equity (worth more points).
    DEBT_TREND_YEARS = 5
    debts = [(r["year"], r["debt"]) for r in rows if r["debt"] and r["debt"] > 0]
    if len(debts) >= 2:
        recent = debts[-(DEBT_TREND_YEARS + 1):]     # last N years + baseline
        d0, d1 = recent[0][1], recent[-1][1]
        try:
            out["debt_trend_pct"] = round((d1 - d0) / d0 * 100, 2)
            out["debt_trend_years"] = recent[-1][0] - recent[0][0]
        except ZeroDivisionError:
            pass

    return out


# ---------------------------------------------------------------------------
# Universe + per-symbol fetch
# ---------------------------------------------------------------------------
# Real US exchanges (MIC codes). Everything else - overwhelmingly OOTC (OTC /
# pink sheets) - is excluded. Measured on the live Finnhub directory:
#     OOTC 13,477 | XNAS 3,187 | XNYS 1,556 | XASE 227 | BATS 1
# i.e. 73% of "Common Stock" entries are OTC shells, foreign F-suffix listings
# and bankrupt Q-suffix tickers with no usable fundamentals. Excluding them cuts
# a full refresh from ~33 hours to ~9 AND produces a far cleaner universe -
# the same cleanup we did for NSE (dropping SME/BE names), applied at the source.
_REAL_US_EXCHANGES = {"XNAS", "XNYS", "XASE", "BATS", "ARCX", "XBOS", "IEXG"}


def _us_symbol_universe() -> List[str]:
    """US common stocks listed on a REAL exchange (one call).

    Excludes: ETFs/funds/warrants/units/rights (by type), OTC and pink-sheet
    listings (by MIC), share-class variants like BRK.A (dot), and tickers over
    5 chars. Note that 5-letter tickers ending in F are typically foreign OTC
    and ending in Q are bankrupt - the MIC filter removes both categories
    wholesale, which is more reliable than suffix-guessing."""
    data = _finnhub_get("stock/symbol", {"exchange": "US"})
    if not data or not isinstance(data, list):
        logger.warning("Finnhub stock/symbol returned nothing - US universe empty")
        return []

    syms, skipped_otc, skipped_type = [], 0, 0
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if (row.get("type") or "").strip() != "Common Stock":
            skipped_type += 1
            continue
        if (row.get("mic") or "").strip().upper() not in _REAL_US_EXCHANGES:
            skipped_otc += 1
            continue
        if "." in sym or len(sym) > 5:
            continue
        syms.append(sym)

    syms = sorted(set(syms))
    logger.info("Finnhub US universe: %d exchange-listed common stocks "
                "(skipped %d OTC/pink-sheet, %d non-common-stock)",
                len(syms), skipped_otc, skipped_type)
    return syms


def fetch_symbol_full(symbol: str) -> Optional[tuple]:
    """Fetch ALL endpoints for one symbol. Returns (FundamentalData, extras).

    6 calls per symbol. `metric` is required (no metrics -> unscoreable, skip);
    every other endpoint degrades gracefully to None so a partial outage or a
    thinly-covered small-cap still yields a usable record."""
    sym = symbol.upper().strip()

    metric_resp = _finnhub_get("stock/metric", {"symbol": sym, "metric": "all"})
    m = (metric_resp or {}).get("metric") or {}
    if not m:
        return None

    profile = _finnhub_get("stock/profile2", {"symbol": sym}) or {}
    fin = _finnhub_get("stock/financials-reported", {"symbol": sym, "freq": "annual"}) or {}
    quote = _finnhub_get("quote", {"symbol": sym}) or {}
    recos = _finnhub_get("stock/recommendation", {"symbol": sym}) or []
    insider = _finnhub_get("stock/insider-transactions", {"symbol": sym}) or {}

    market_cap = _to_usd(m.get("marketCapitalization"), "marketCapitalization")
    if not market_cap:
        market_cap = _to_usd(profile.get("marketCapitalization"), "marketCapitalization")
    if not market_cap:
        return None

    roe    = _num(_pick(m, _ALIASES["roe"]))
    de     = _num(_pick(m, _ALIASES["debt_to_equity"]))
    growth = _num(_pick(m, _ALIASES["revenue_growth"]))
    pe     = _num(_pick(m, _ALIASES["pe_ratio"]))
    ps     = _num(_pick(m, _ALIASES["ps_ratio"]))
    cr     = _num(_pick(m, _ALIASES["current_ratio"]))
    pgrow  = _num(_pick(m, _ALIASES["profit_growth"]))

    derived = _derive_from_financials(fin.get("data") or [])

    # Prefer REPORTED revenue/net income (from the statements) over anything
    # derived from multiples - reported figures are the real numbers, and a
    # P/E-derived estimate was measurably ~15% off on AAPL.
    revenue_total = net_income_total = op_income_total = None
    reports = fin.get("data") or []
    if reports:
        rep0 = (reports[0].get("report") or {})
        revenue_total = _find_line(rep0.get("ic"), _IC_REVENUE)
        net_income_total = _find_line(rep0.get("ic"), _IC_NETINC)
        op_income_total = _find_line(rep0.get("ic"), _IC_OPINC)

    # Fallback only when statements are unavailable: revenue = mktcap / P/S
    if revenue_total is None and ps and ps > 0:
        revenue_total = round(market_cap / ps, 2)
    if net_income_total is None and pe and pe > 0:
        net_income_total = round(market_cap / pe, 2)

    extras = {
        **derived,
        "ps_ratio": ps,
        # both revenue sources kept so check_data_integrity can cross-verify
        "reported_revenue": revenue_total,
        "vendor_revenue": (round(market_cap / ps, 2) if ps and ps > 0 else None),
        "industry": profile.get("finnhubIndustry"),
        "exchange": profile.get("exchange"),
        "ipo": profile.get("ipo"),
        "weburl": profile.get("weburl"),
        "logo": profile.get("logo"),
        "shares_outstanding": (_num(profile.get("shareOutstanding")) or 0) * 1e6 or None,
        "float_shares": (_num(profile.get("floatingShare")) or 0) * 1e6 or None,
        "price": _num(quote.get("c")),
        "price_change_pct": _num(quote.get("dp")),
        # --- sentiment: STORED but deliberately NOT scored ---
        "analyst": (recos[0] if isinstance(recos, list) and recos else None),
        "insider_recent": (insider.get("data") or [])[:20] if isinstance(insider, dict) else [],
        "_fetched": datetime.now().isoformat(),
    }

    # net/operating margin from metrics if the statements didn't give them
    if extras.get("net_margin") is None:
        extras["net_margin"] = _num(_pick(m, _ALIASES["net_margin"]))
    if extras.get("operating_margin") is None:
        extras["operating_margin"] = _num(_pick(m, _ALIASES["op_margin"]))
    # NOTE: deliberately do NOT backfill revenue_cagr from the vendor's
    # multi-year aggregate here. When filings don't support a CAGR we WANT it
    # left unset, so the engine falls back to the current YoY figure. Filling it
    # with revenueGrowth5Y is what made a 2024 spin-off score 0/20 on growth
    # despite +528% actual growth - the stale aggregate wasn't None, so it won.
    # Keep it visible under a separate key for reference/debugging only.
    extras["vendor_revenue_cagr_5y"] = _num(_pick(m, _ALIASES["revenue_cagr"]))

    try:
        fd = FundamentalData(
            symbol=sym,
            name=profile.get("name") or sym,
            # field is *_cr for schema reuse, but US stores RAW USD - dividing
            # by 1e7 here would render "$45,000 Cr" nonsense in the UI.
            market_cap_cr=round(market_cap, 2),
            pe_ratio=pe,
            roe=roe,
            debt_to_equity=de,
            promoter_holding=None,          # not a US concept
            revenue_growth=growth,
            profit_growth=pgrow,
            current_ratio=cr,
            revenue_cr=round(revenue_total, 2) if revenue_total else None,
            net_income_cr=round(net_income_total, 2) if net_income_total else None,
            operating_income_cr=round(op_income_total, 2) if op_income_total else None,
            fcf_cr=extras.get("fcf"),
            revenue_growth_calc=extras.get("revenue_cagr") or growth,
            profit_growth_calc=pgrow,
            score=0.0,
            last_updated=datetime.now().strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        logger.warning("US FundamentalData build failed for %s: %s", sym, exc)
        return None

    try:
        from data_us.fundamental_engine_us import compute_score_with_breakdown_us
        fd.score, breakdown = compute_score_with_breakdown_us(fd, extras)
        extras["score_breakdown"] = breakdown
    except Exception as exc:
        logger.debug("US score engine failed for %s: %s", sym, exc)
        fd.score = 0.0

    return fd, extras


# Back-compat alias (radar / older callers may import this name)
def _fetch_one_symbol(symbol: str) -> Optional[FundamentalData]:
    res = fetch_symbol_full(symbol)
    return res[0] if res else None


# ---------------------------------------------------------------------------
# Batch fetch - incremental, resumable, rate-limited
# ---------------------------------------------------------------------------
def _fetch_finnhub_all() -> List[FundamentalData]:
    by_symbol = _load_symbol_cache()
    extras_all = load_extras()
    symbols = _us_symbol_universe()
    if not symbols:
        return []

    cache_days = float(getattr(settings, "FUNDAMENTAL_REFRESH_DAYS", 21))
    to_fetch = []
    for sym in symbols:
        rec = by_symbol.get(sym)
        if rec and rec.get("_ts"):
            try:
                if (datetime.now() - datetime.fromisoformat(rec["_ts"])).days < cache_days:
                    continue
            except Exception:
                pass
        to_fetch.append(sym)

    est_min = len(to_fetch) * 6 / 55.0     # 6 calls per symbol at 55 calls/min
    logger.info("US fundamentals: %d/%d symbols to fetch (~%.0f min at 6 calls each)",
                len(to_fetch), len(symbols), est_min)

    clear_stop()        # a previous stop must not abort this new run
    _progress.update({
        "running": True, "total": len(to_fetch), "done": 0, "fetched": 0,
        "failed": 0, "current": "", "started_at": datetime.now().isoformat(),
        "finished_at": None, "stopping": False,
        "message": f"fetching {len(to_fetch)} symbols x6 endpoints (~{est_min/60:.1f} hrs)",
    })

    fetched = failed = 0
    stopped = False
    for i, sym in enumerate(to_fetch):
        # Cooperative stop: checked BETWEEN symbols so the one in flight always
        # finishes and gets saved. Nothing is lost, and a later refresh resumes
        # from here because cached symbols are skipped.
        if _stop_requested.is_set():
            stopped = True
            logger.info("US fetch stopped by user after %d/%d symbols", i, len(to_fetch))
            break
        _progress["current"] = sym
        try:
            res = fetch_symbol_full(sym)
        except RuntimeError:
            raise                       # bad API key - surface now, don't grind for hours
        except Exception as exc:
            logger.debug("US fetch error %s: %s", sym, exc)
            res = None

        if res:
            fd, extras = res
            by_symbol[sym] = {**fd.model_dump(), "_ts": datetime.now().isoformat()}
            extras_all[sym] = extras
            _save_symbol_cache(by_symbol)                    # incremental
            _atomic_write_json(EXTRAS_CACHE_FILE, extras_all)  # safe to Ctrl+C
            fetched += 1
        else:
            failed += 1

        _progress.update({"done": i + 1, "fetched": fetched, "failed": failed})
        if (i + 1) % 25 == 0:
            logger.info("US fundamentals: %d/%d (%d ok, %d failed)",
                        i + 1, len(to_fetch), fetched, failed)

    clear_stop()
    _progress.update({
        "running": False, "current": "", "finished_at": datetime.now().isoformat(),
        "stopping": False,
        "message": (f"stopped by user - {fetched} fetched, {failed} failed "
                    f"(click Refresh to resume)" if stopped
                    else f"complete - {fetched} fetched, {failed} failed"),
    })
    logger.info("US fundamentals %s: %d fetched, %d failed",
                "stopped" if stopped else "complete", fetched, failed)

    out = []
    for sym, rec in by_symbol.items():
        try:
            out.append(FundamentalData(**{k: v for k, v in rec.items() if not k.startswith("_")}))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Filter / universe assembly
# ---------------------------------------------------------------------------
def _passes_filter_us(s: FundamentalData) -> bool:
    min_cap = float(getattr(settings, "MIN_MARKET_CAP_USD", 300_000_000.0))
    if s.market_cap_cr < min_cap:
        return False
    if s.roe is not None and s.roe < settings.SCREENER_MIN_ROE:
        return False
    if s.debt_to_equity is not None and s.debt_to_equity > settings.SCREENER_MAX_DE:
        return False
    return True


def refresh_universe_us() -> List[FundamentalData]:
    stocks = _fetch_finnhub_all()
    approved = [s for s in stocks if _passes_filter_us(s)]
    approved.sort(key=lambda s: s.score, reverse=True)
    _atomic_write_json(CACHE_FILE, [s.model_dump() for s in approved])
    logger.info("US universe rebuilt: %d approved / %d total", len(approved), len(stocks))
    return approved


def approved_from_cache_us() -> List[FundamentalData]:
    """Rebuild the approved list from cache with NO network calls."""
    out = []
    for sym, rec in _load_symbol_cache().items():
        try:
            fd = FundamentalData(**{k: v for k, v in rec.items() if not k.startswith("_")})
        except Exception:
            continue
        if _passes_filter_us(fd):
            out.append(fd)
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def load_universe_us() -> List[FundamentalData]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return [FundamentalData(**row) for row in json.load(f)]
        except Exception as exc:
            logger.warning("US universe file read failed: %s - rebuilding from cache", exc)
    return approved_from_cache_us()


def deep_dive_us(symbol: str) -> Optional[dict]:
    """Fresh full fetch for one stock, including extras (industry, FCF, analyst
    consensus, insider activity, score breakdown)."""
    res = fetch_symbol_full(symbol)
    if not res:
        return None
    fd, extras = res
    data = fd.model_dump()
    data["extras"] = extras
    return data
