"""
Rush Algo — Fundamental Screener
Fetches data from Screener.in (free, no API key needed).
Filters stocks by: market cap > ₹1000Cr, ROE > 12%, D/E < 1.5,
promoter holding > 40%, revenue growth > 10%.
Refreshes weekly and caches results to disk.
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

import requests

from config import settings
from models.schemas import FundamentalData

logger    = logging.getLogger(__name__)

# CACHE_DIR was previously the RELATIVE path "data_cache", which resolves against
# whatever the process's current working directory happens to be at runtime. That
# is the root cause of "the cache/tombstones don't persist": uvicorn --reload (and
# launching from C:\rush-algo-fixed vs C:\rush-algo-fixed\backend) can give the
# save-process and the next startup-process DIFFERENT working directories, so the
# write lands in one data_cache folder and the next read looks in another. The
# data was being saved correctly — just to a folder the next run wasn't reading.
#
# Fix: anchor to an ABSOLUTE path derived from this file's own location. This file
# is backend/data/fundamental.py, so its parent's parent is the backend dir, and
# data_cache sits directly under it. Now every process reads/writes the exact same
# folder no matter where it was launched from.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_BACKEND_DIR, "data_cache")
CACHE_FILE= os.path.join(CACHE_DIR, "fundamental_universe.json")

# Live scan progress, polled by the frontend via GET /api/universe/progress.
# In-memory only (resets on restart) - reflects the CURRENT run. Actual data
# safety comes from the incremental per-symbol cache, not from this.
_scan_progress = {
    "running": False, "total": 0, "done": 0, "fetched": 0, "failed": 0,
    "current": "", "started_at": None, "finished_at": None, "message": "idle",
    "stopping": False,
}


# Cooperative stop flag. Checked BETWEEN symbols so the one in flight always
# finishes and is saved - a stop never loses work. Resuming is just triggering
# a refresh again: cached symbols are skipped, so it continues where it left off.
_stop_requested = threading.Event()


def request_scan_stop() -> dict:
    """Ask a running universe scan to stop after the current symbol completes."""
    if _scan_progress.get("running"):
        _stop_requested.set()
        _scan_progress["stopping"] = True
        _scan_progress["message"] = "stopping after current symbol..."
    return get_scan_progress()


def clear_scan_stop() -> None:
    _stop_requested.clear()
    _scan_progress["stopping"] = False


def get_scan_progress() -> dict:
    """Snapshot of the current/last universe scan for the frontend progress bar."""
    p = dict(_scan_progress)
    p["pct"] = round(p["done"] / p["total"] * 100, 1) if p["total"] else 0.0
    return p
HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _cache_valid() -> bool:
    """True if a usable universe file exists on disk.

    Deliberately does NOT consider the file's AGE. FUNDAMENTAL_REFRESH_DAYS
    controls PER-STOCK staleness during a fetch you explicitly trigger - it is
    not a schedule. Age-gating here made startup auto-fetch from IndianAPI the
    moment the file aged past the threshold, which burned quota unprompted and
    competed with other work. Fetching now happens only when the user asks."""
    return os.path.exists(CACHE_FILE) and os.path.getsize(CACHE_FILE) > 2


def approved_from_cache() -> List[FundamentalData]:
    """
    Return the approved universe from whatever is ALREADY on disk, without ever
    triggering a (blocking) fetch. Used by the dashboard so it stays responsive and
    shows the universe growing as the background scan progresses.

    Prefers the assembled universe file if present; otherwise builds the approved
    list on the fly from the per-symbol cache (which the running scan keeps filling).
    """
    # 1) assembled universe file, if the scan has finished at least once
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            stocks = [FundamentalData(**d) for d in data]
            # Apply the revenue-scale shortlist floor here too: the pre-built file may
            # contain sub-scale stocks from before this rule existed, so we filter on
            # read. (Deep Dives reads the per-symbol cache directly and is unaffected —
            # any stock stays searchable there.)
            min_rev = getattr(settings, "MIN_REVENUE_CR", 0.0)
            if min_rev > 0:
                stocks = [s for s in stocks
                          if getattr(s, "revenue_cr", None) is not None
                          and s.revenue_cr >= min_rev]
            if stocks:
                return stocks
    except Exception as exc:
        logger.debug("approved_from_cache: universe file read failed: %s", exc)

    # 2) otherwise derive from the per-symbol cache that the scan fills incrementally
    try:
        by_symbol = _load_symbol_cache()
        out: List[FundamentalData] = []
        for sym, rec in by_symbol.items():
            try:
                clean = {k: v for k, v in rec.items() if not k.startswith("_")}
                fd = FundamentalData(**clean)
            except Exception:
                continue
            if _passes_filter(fd):
                out.append(fd)
        out.sort(key=lambda s: s.score or 0, reverse=True)
        return out
    except Exception as exc:
        logger.debug("approved_from_cache: symbol cache read failed: %s", exc)
        return []


def load_universe() -> List[FundamentalData]:
    """Load the approved fundamental universe WITHOUT ever hitting the network.

    Resolution order:
      1. the assembled universe file, if present
      2. otherwise rebuild locally from the per-symbol cache
         (fundamentals_by_symbol.json) - this is the real data; the universe
         file is only a derived ranked view of it, so it can always be rebuilt
         offline by re-scoring and re-filtering what's already on disk
      3. only if BOTH are missing (a genuine first run) does it fetch

    Startup must never fetch just because data is old - a refresh is triggered
    by the user, and FUNDAMENTAL_REFRESH_DAYS then decides which individual
    stocks are stale enough to re-pull."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if _cache_valid():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            stocks = [FundamentalData(**d) for d in data]
            logger.info("Fundamental universe loaded from cache: %d stocks", len(stocks))
            return stocks
        except Exception as exc:
            logger.warning("Universe file unreadable (%s) - rebuilding from per-symbol cache", exc)

    # No usable universe file: rebuild from the per-symbol cache, offline.
    try:
        if os.path.exists(SYMBOL_CACHE_FILE) and os.path.getsize(SYMBOL_CACHE_FILE) > 2:
            stocks = approved_from_cache()
            if stocks:
                logger.info("Universe rebuilt locally from per-symbol cache: %d stocks "
                            "(no network calls)", len(stocks))
                try:
                    _atomic_write_json(CACHE_FILE, [s.model_dump() for s in stocks])
                except Exception as exc:
                    logger.debug("Could not persist rebuilt universe: %s", exc)
                return stocks
            logger.warning("Per-symbol cache present but no stocks passed the filters")
    except Exception as exc:
        logger.warning("Local rebuild failed (%s)", exc)

    # Genuine first run - nothing cached at all.
    logger.info("No cached fundamentals found - performing initial fetch")
    return refresh_universe()


def refresh_universe() -> List[FundamentalData]:
    """Fetch fresh fundamental data and rebuild approved universe."""
    if not _try_acquire_scan_lock():
        logger.warning(
            "A fundamental scan is already running elsewhere (e.g. the previous "
            "--reload worker is still finishing up, or a refresh is already in "
            "flight) — skipping a duplicate scan instead of racing it. Serving "
            "whatever is currently cached.")
        return approved_from_cache()
    try:
        logger.info("Refreshing fundamental universe from Screener.in...")
        stocks = _fetch_screener()
        approved = [s for s in stocks if _passes_filter(s)]
        approved.sort(key=lambda s: s.score, reverse=True)
        _atomic_write_json(CACHE_FILE, [s.model_dump() for s in approved])
        logger.info("Fundamental universe saved: %d approved stocks", len(approved))
        return approved
    finally:
        _release_scan_lock()


def _fetch_screener() -> List[FundamentalData]:
    """
    Screener.in fetch.

    NOTE: Screener.in's /screens/<id>/ path serves an HTML page, not JSON, on a
    plain GET — so the previous implementation that called resp.json() on it always
    threw, got swallowed, and silently fell back to the curated list on EVERY run.
    The flag below makes that behaviour explicit and controllable instead of hidden.

    Screener does not expose a clean public JSON API for screen results without a
    logged-in session + CSV export. Until a real data source / broker fundamental
    feed is wired in, we use the curated NIFTY-500 list by default and log it loudly
    so it's never a silent surprise. Set settings.SCREENER_LIVE=True only once a
    working authenticated fetch is implemented in _fetch_screener_live().
    """
    if getattr(settings, "SCREENER_LIVE", False):
        # Prefer IndianAPI (real India fundamentals incl. promoter holding) if a key
        # is set; otherwise fall back to the yfinance-based fetch.
        if getattr(settings, "INDIANAPI_KEY", ""):
            try:
                stocks = _fetch_indianapi()
                if stocks:
                    logger.info("IndianAPI fundamentals: %d stocks", len(stocks))
                    return stocks
                logger.warning("IndianAPI returned no stocks — trying yfinance")
            except Exception as exc:
                logger.warning("IndianAPI fetch failed (%s) — trying yfinance", exc)
        try:
            stocks = _fetch_screener_live()
            if stocks:
                logger.info("Screener live fetch: %d stocks", len(stocks))
                return stocks
            logger.warning("Screener live fetch returned no stocks — using curated fallback")
        except Exception as exc:
            logger.warning("Screener live fetch failed (%s) — using curated fallback", exc)
    else:
        logger.info("Screener live fetch disabled (SCREENER_LIVE=False) — "
                    "using curated NIFTY-500 fundamental list")

    return _nifty500_fallback()


def _indianapi_symbol_universe(base: str, headers: dict) -> List[str]:
    """
    Build the symbol universe to fetch fundamentals for.

    PRIMARY: IndianAPI's static all_stocks.json — the full list of stocks they
    actually support, with nse-code. It's a static file (does NOT cost an API
    request / isn't rate-limited), and matches their coverage exactly so we never
    waste /stock requests on symbols they don't have. Count = whatever it holds.

    FALLBACKS: NSE's own EQUITY_L.csv (if the static list fails), then the curated
    large-caps as a floor so the app always has something.
    """
    symbols: set = set()

    # Local cache of all_stocks.json so a network blip at the START of a run
    # (before any per-stock work has happened) doesn't stall the whole scan.
    # Refreshed if older than 7 days OR missing; otherwise served instantly,
    # offline-safe, and falls back to this cache on any network failure.
    _all_stocks_cache = os.path.join(CACHE_DIR, "all_stocks_cache.json")

    def _load_all_stocks_cache():
        try:
            if os.path.exists(_all_stocks_cache):
                with open(_all_stocks_cache, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _save_all_stocks_cache(data):
        try:
            _atomic_write_json(_all_stocks_cache, data)
        except Exception as exc:
            logger.debug("all_stocks cache write failed: %s", exc)

    # 1) PRIMARY: IndianAPI static all-stocks list (free, complete).
    #    Retries a few times (network blips are transient), then falls back to
    #    the last-known-good local cache before giving up on this source.
    fetched_fresh = False
    for attempt in range(3):
        try:
            r = requests.get(f"{base}/static/all_stocks.json", headers=headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                for row in data if isinstance(data, list) else []:
                    code = (row.get("nse-code") or row.get("nseCode") or "").strip().upper()
                    if code:
                        symbols.add(code)
                logger.info("Universe: %d symbols from IndianAPI all_stocks.json", len(symbols))
                if symbols:
                    _save_all_stocks_cache(sorted(symbols))
                    fetched_fresh = True
                break
        except Exception as exc:
            logger.warning("IndianAPI all_stocks.json failed (attempt %d/3): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(3 * (attempt + 1))   # 3s, 6s backoff before retry

    if not fetched_fresh:
        cached_list = _load_all_stocks_cache()
        if cached_list:
            symbols.update(cached_list)
            logger.info("Universe: %d symbols from LOCAL CACHE (network unavailable, "
                        "using last-known-good all_stocks list)", len(symbols))

    # 2) FALLBACK: NSE master list (only if the static list gave us little).
    if len(symbols) < 100:
        try:
            from data.nse_universe import get_nse_symbols
            nse = get_nse_symbols()
            symbols.update(nse)
            logger.info("Universe: +%d from NSE master list (fallback)", len(nse))
        except Exception as exc:
            logger.warning("NSE master list fallback failed: %s", exc)

    # 3) FLOOR: curated large-caps so we never end up empty.
    if len(symbols) < 50:
        symbols.update(s.symbol for s in _nifty500_fallback())
        logger.info("Universe small — added curated seed floor (%d total)", len(symbols))

    logger.info("Universe symbol list: %d total candidate symbols", len(symbols))
    return sorted(symbols)


def _extract_symbols_into(data, out: set) -> None:
    """Recursively pull NSE ticker symbols from any IndianAPI JSON response shape."""
    def clean(tkr: str) -> Optional[str]:
        if not tkr or not isinstance(tkr, str):
            return None
        t = tkr.upper().strip()
        # normalize "RELIANCE.NS" / "RELIANCE.BO" / "NSE:RELIANCE" → "RELIANCE"
        t = t.replace("NSE:", "").replace("BSE:", "")
        t = t.replace(".NS", "").replace(".BO", "").replace("-EQ", "").strip()
        # plausible ticker: letters/digits, reasonable length
        if t and 1 <= len(t) <= 20 and t.replace("&", "").replace("-", "").isalnum():
            return t
        return None

    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                lk = k.lower()
                # common ticker-bearing keys across IndianAPI shapes
                if lk in ("ticker", "nse-code", "nsecode", "nseric", "exchangecodensi",
                          "symbol", "nse_ticker") and isinstance(v, str):
                    c = clean(v)
                    if c:
                        out.add(c)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def _fetch_indianapi() -> List[FundamentalData]:
    """
    Fetch real fundamentals from IndianAPI (indianapi.in).

    IMPORTANT: the symbol list comes from NSE's full equity master list (count is
    whatever NSE lists — NOT hardcoded), supplemented by IndianAPI's small list
    endpoints, with the curated large-caps as a floor.

    TWO-PHASE: this used to check the cache and make a network call in the SAME
    pass through all symbols, which made the progress log misleading — "550/3072"
    looks like 18% done, but most of that 550 was instant cache hits, so it told
    you almost nothing about how much SLOW (network) work was actually left.

    Phase 1 is a pure in-memory cache check across every candidate symbol — no
    network calls at all, takes milliseconds even for thousands of symbols — and
    tells you up front exactly how many actually need fetching.
    Phase 2 only makes network calls for that (much smaller) remaining set, so
    the progress log directly reflects real, slow work instead of being diluted
    by thousands of free cache hits.

    INCREMENTAL CACHE (crash/disconnect-safe): each stock's fundamentals are saved
    to disk the MOMENT they're fetched. If the backend is killed, the network drops,
    or you stop mid-scan, every stock fetched so far is already persisted — so on
    the next run those are served from cache and NOT re-requested. This means you
    never lose fetched data and never waste your monthly request quota re-fetching
    what you already have. A ~2000-stock scan can therefore be done across several
    runs and will simply resume where it left off.
    """
    base = settings.INDIANAPI_BASE.rstrip("/")
    headers = {"X-API-Key": settings.INDIANAPI_KEY}
    symbols = _indianapi_symbol_universe(base, headers)
    by_symbol = _load_symbol_cache()
    cache_days = int(getattr(settings, "FUNDAMENTAL_REFRESH_DAYS", 7))
    delay = float(getattr(settings, "SCREENER_FETCH_DELAY_SEC", 1.0))
    stocks: List[FundamentalData] = []
    from_cache = 0

    empty_cooldown = int(getattr(settings, "FUNDAMENTAL_EMPTY_RECHECK_DAYS", 30))

    # ── PHASE 1: cache-only verification, zero network calls ──────────────────
    # RECOVERY: an earlier run with a buggy parser saved records that are fresh
    # but EMPTY (null roe/debt/promoter/growth). Treat such records as incomplete
    # and re-fetch them so the corrected parser can fill them in — while genuinely
    # complete cached records are served straight from cache without a request.
    #
    # TOMBSTONE / QUOTA SAVER: some stocks fetch fine (200 OK) but IndianAPI
    # genuinely has no ROE/debt-equity for them (small/illiquid/newly-listed
    # names). Without this, those ~600 stocks would be re-fetched on EVERY scan
    # forever — burning ~600 API requests each run for the same empty result. So
    # when a fresh fetch comes back still-incomplete, we stamp the record with
    # `_checked_empty`. Here in phase 1 we skip re-fetching such a record until
    # `empty_cooldown` days have passed (default 30) — long enough to save quota,
    # short enough to eventually pick up coverage if IndianAPI adds the data later.
    skipped_empty = 0
    to_fetch: List[str] = []
    for sym in symbols:
        cached = _fresh_cached_symbol(by_symbol, sym, cache_days)
        if cached is not None and _is_complete(cached):
            stocks.append(cached)
            from_cache += 1
            continue
        # incomplete — but was it recently confirmed empty by the API itself?
        if _recently_checked_empty(by_symbol, sym, empty_cooldown):
            if cached is not None:
                stocks.append(cached)   # keep whatever partial data we do have
                from_cache += 1
            skipped_empty += 1
            continue
        to_fetch.append(sym)

    logger.info("IndianAPI: cache check complete — %d/%d already cached & complete, "
                "%d confirmed-empty by API (skipped, recheck in <%dd), "
                "%d need fetching from the network (incremental cache — safe to "
                "interrupt/resume)",
                from_cache, len(symbols), skipped_empty, empty_cooldown, len(to_fetch))

    # ── PHASE 2: network fetch, ONLY for what phase 1 couldn't already serve ──
    clear_scan_stop()      # a previous stop must not abort this new run
    _scan_progress.update({
        "running": True, "total": len(to_fetch), "done": 0, "fetched": 0,
        "failed": 0, "current": "", "started_at": datetime.now().isoformat(),
        "finished_at": None, "stopping": False,
        "message": f"fetching {len(to_fetch)} symbols ({from_cache} served from cache)",
    })
    fetched = failed = now_complete = still_incomplete = 0
    scan_stopped = False
    for i, sym in enumerate(to_fetch):
        # Cooperative stop: checked BETWEEN symbols so whatever is in flight
        # finishes and gets saved. Nothing is lost; a later refresh resumes from
        # here because already-cached symbols are skipped.
        if _stop_requested.is_set():
            scan_stopped = True
            logger.info("NSE universe scan stopped by user after %d/%d symbols",
                        i, len(to_fetch))
            break
        _scan_progress["current"] = sym
        _touch_scan_lock()   # keep the lock heartbeat fresh while this scan is alive
        # Only non-None for "fresh but incomplete" leftovers from the buggy old
        # parser — anything stale or missing is None here, same as before.
        cached = _fresh_cached_symbol(by_symbol, sym, cache_days)
        try:
            r = requests.get(f"{base}/stock", params={"name": sym}, headers=headers, timeout=20)
            if r.status_code == 429:
                # rate-limited — back off and retry once
                logger.warning("IndianAPI 429 at %s — backing off", sym)
                time.sleep(max(2.0, delay * 3))
                r = requests.get(f"{base}/stock", params={"name": sym}, headers=headers, timeout=20)
            if r.status_code != 200:
                # keep any existing (even if incomplete) cached record rather than lose it
                if cached is not None:
                    stocks.append(cached); from_cache += 1
                else:
                    failed += 1
                time.sleep(delay); continue
            data = r.json()
        except Exception as exc:
            logger.debug("IndianAPI %s failed: %s", sym, exc)
            if cached is not None:
                stocks.append(cached); from_cache += 1
            else:
                failed += 1
            time.sleep(delay); continue

        fd = _parse_indianapi(sym, data)
        if fd:
            # NEW: comprehensive six-category fundamental score over the full raw data.
            try:
                from data.fundamental_engine import compute_fundamental_score
                breakdown = compute_fundamental_score(data)
                fd.score = breakdown["score"]
            except Exception as exc:
                logger.warning("engine score failed for %s (%s) — falling back", sym, exc)
                breakdown = None
                fd.score = score_stock(fd)
            stocks.append(fd)
            # INCREMENTAL SAVE: persist this stock immediately so it's never lost.
            # Store the FULL raw response too (store wide, score narrow) so we never
            # have to re-fetch to get a field we didn't extract this time.
            rec = {**fd.model_dump(), "_ts": datetime.now().isoformat(), "_raw": data}
            if breakdown is not None:
                rec["_breakdown"] = breakdown
            # Direct visibility into whether the roe/de dig() fallback actually
            # helped: did this re-fetch newly satisfy _is_complete(), or is the
            # data genuinely not available from the API for this stock?
            if _is_complete(fd):
                now_complete += 1
            else:
                # TOMBSTONE: fetch succeeded but API has no roe/de for this stock.
                # Stamp it so phase 1 stops re-fetching it every scan (quota saver).
                rec["_checked_empty"] = datetime.now().isoformat()
                still_incomplete += 1
            by_symbol[sym] = rec
            _save_symbol_cache(by_symbol)
            fetched += 1
        elif cached is not None:
            stocks.append(cached); from_cache += 1   # keep old record if re-parse fails
        else:
            failed += 1

        _scan_progress.update({"done": i + 1, "fetched": fetched, "failed": failed})
        # progress log now reflects real (slow) work remaining, not cache hits
        if (i + 1) % 25 == 0 or (i + 1) == len(to_fetch):
            logger.info("IndianAPI progress: %d/%d new fetches (%d succeeded -> %d now "
                        "complete, %d still missing roe/de from the API, %d failed) "
                        "— %d total served from cache so far",
                        i + 1, len(to_fetch), fetched, now_complete, still_incomplete,
                        failed, from_cache)
        time.sleep(delay)

    _save_symbol_cache(by_symbol)
    clear_scan_stop()
    _scan_progress.update({
        "running": False, "current": "", "finished_at": datetime.now().isoformat(),
        "stopping": False,
        "message": (f"stopped by user - {fetched} fetched, {failed} failed "
                    f"(click Refresh to resume)" if scan_stopped
                    else f"complete - {fetched} fetched, {failed} failed"),
    })
    logger.info("IndianAPI fundamentals complete: %d new (%d now complete, %d still "
                "incomplete after fetch — likely a genuine API data gap, not a parsing "
                "bug), %d from cache, %d failed (total usable: %d)",
                fetched, now_complete, still_incomplete, from_cache, failed, len(stocks))
    return stocks


def _extract_financials(data: dict) -> dict:
    """
    Pull ABSOLUTE figures (₹ crore) and self-computed growth from IndianAPI's real
    structure. Returns a dict of the new fields; all keys may be None if absent.

    UNIT HANDLING (verified against the live Reliance response — two blocks differ!):
      - financials[].INC/BAL/CAS  → ALREADY in ₹ crore. Do NOT divide.
            (Reliance FY26 Revenue reads 1075675 = ₹10,75,675 cr exactly; NetIncome
             80775 = ₹80,775 cr exactly.)
      - keyMetrics.incomeStatement & keyMetrics.financialstrength → in ₹ '0.1 lakh',
            i.e. 10x the crore value. Divide by 10.
            (Reliance revenueTrailing12Month reads 10756750 → ÷10 = 1,075,675 cr; FCF
             617540 → ÷10 = 61,754 cr.)
      - keyMetrics.priceandVolume.marketCap → ALREADY in ₹ crore (handled elsewhere).
    Getting this wrong silently corrupts every downstream filter, so it's verified
    against known Reliance figures in the test before trusting it.
    """
    KM_TO_CR = 10.0   # keyMetrics incomeStatement/financialstrength → ÷10 = ₹ crore

    def km_section(name):
        km = data.get("keyMetrics") or {}
        arr = km.get(name)
        return arr if isinstance(arr, list) else []

    def km_val(section, *keys):
        for item in km_section(section):
            if isinstance(item, dict):
                k = str(item.get("key", "")).lower().rstrip(")")
                for want in keys:
                    if k == want.lower():
                        return _clean_num(item.get("value"))
        # loose contains
        for item in km_section(section):
            if isinstance(item, dict):
                k = str(item.get("key", "")).lower()
                for want in keys:
                    if want.lower() in k:
                        return _clean_num(item.get("value"))
        return None

    out = {
        "revenue_cr": None, "net_income_cr": None, "operating_income_cr": None,
        "fcf_cr": None, "revenue_growth_calc": None, "profit_growth_calc": None,
    }

    # --- Absolute revenue / income (TTM preferred) from keyMetrics.incomeStatement ---
    #     (these are in ₹ 0.1-lakh → ÷10 for ₹ crore)
    # --- KeyMetrics source (÷10). Kept as a FALLBACK / cross-check, not primary. ---
    km_rev = km_val("incomeStatement", "revenueTrailing12Month", "revenueMostRecentFiscalYear")
    km_ni  = km_val("incomeStatement", "netIncomeAvailableToCommonTrailing12Months",
                    "netIncomeAvailableToCommonMostRecentFiscalYear")

    # --- Free cash flow (₹ cr) from financialstrength (÷10). No financials[] equiv,
    #     so this one relies on keyMetrics units. Guarded below by magnitude check. ---
    fcf = km_val("financialstrength", "freeCashFlowtrailing12Month",
                 "freeCashFlowMostRecentFiscalYear")
    if fcf is not None:
        out["fcf_cr"] = round(fcf / KM_TO_CR, 2)

    # --- Annual series from financials[]: ALREADY in ₹ crore, do NOT divide.
    #     This is the PRIMARY, division-free source for revenue / income / growth. ---
    annual = {}
    for blk in (data.get("financials") or []):
        if not isinstance(blk, dict) or blk.get("Type") != "Annual":
            continue
        fy = str(blk.get("FiscalYear") or "")
        fmap = blk.get("stockFinancialMap") or {}
        inc = fmap.get("INC") or []
        def inc_val(*keys):
            for it in inc:
                if isinstance(it, dict):
                    k = str(it.get("key", "")).lower()
                    for want in keys:
                        if k == want.lower():
                            return _clean_num(it.get("value"))
            return None
        rev_y = inc_val("revenue", "totalrevenue")
        ni_y  = inc_val("netincome", "netincomeaftertaxes")
        oi_y  = inc_val("operatingincome")
        if fy and rev_y is not None:
            annual[fy] = {"rev": rev_y, "ni": ni_y, "oi": oi_y}

    if annual:
        years = sorted(annual.keys())
        latest = annual[years[-1]]
        # PRIMARY: take revenue / income / operating income from financials[] (no ÷)
        if latest.get("rev") is not None:
            out["revenue_cr"] = round(latest["rev"], 2)
        if latest.get("ni") is not None:
            out["net_income_cr"] = round(latest["ni"], 2)
        if latest.get("oi") is not None:
            out["operating_income_cr"] = round(latest["oi"], 2)
        # YoY growth from the two most recent annual years (real, self-computed)
        if len(years) >= 2:
            cur, prev = annual[years[-1]], annual[years[-2]]
            if cur.get("rev") and prev.get("rev") and prev["rev"] != 0:
                out["revenue_growth_calc"] = round((cur["rev"] - prev["rev"]) / abs(prev["rev"]) * 100, 2)
            if cur.get("ni") is not None and prev.get("ni") not in (None, 0):
                out["profit_growth_calc"] = round((cur["ni"] - prev["ni"]) / abs(prev["ni"]) * 100, 2)

    # --- FALLBACK: if financials[] had no revenue/income, use keyMetrics (÷10) ---
    if out["revenue_cr"] is None and km_rev is not None:
        out["revenue_cr"] = round(km_rev / KM_TO_CR, 2)
    if out["net_income_cr"] is None and km_ni is not None:
        out["net_income_cr"] = round(km_ni / KM_TO_CR, 2)

    # --- UNIT SANITY GUARD (robustness across all stocks, not just Reliance) ---
    # We have two independent revenue sources: financials[] (no ÷) and keyMetrics (÷10).
    # When BOTH exist they must agree to within ~5%. If they don't, IndianAPI's units
    # for THIS stock differ from the verified pattern — flag it so a unit quirk can
    # never silently corrupt a stock's data. The financials[] value is kept (it needs
    # no unit assumption), and a marker is stored for later review.
    if out["revenue_cr"] is not None and km_rev is not None:
        km_rev_cr = km_rev / KM_TO_CR
        if out["revenue_cr"] > 0:
            disagree = abs(km_rev_cr - out["revenue_cr"]) / out["revenue_cr"]
            if disagree > 0.05:
                out["_unit_mismatch"] = {
                    "financials_rev_cr": out["revenue_cr"],
                    "keymetrics_rev_div10_cr": round(km_rev_cr, 2),
                    "keymetrics_rev_raw": km_rev,
                    "disagreement_pct": round(disagree * 100, 1),
                }

    # --- FCF magnitude guard: FCF shouldn't exceed revenue by a wild margin. If it
    #     does, the ÷10 assumption likely failed for this stock's keyMetrics block. ---
    if out["fcf_cr"] is not None and out["revenue_cr"]:
        if abs(out["fcf_cr"]) > out["revenue_cr"] * 3:
            out["_fcf_suspect"] = {"fcf_cr": out["fcf_cr"], "revenue_cr": out["revenue_cr"]}

    return out


# ── SYMBOL IDENTITY GUARD ─────────────────────────────────────────────────────
# IndianAPI's /stock?name=X does FUZZY matching and silently returns a DIFFERENT
# company when it has no exact ticker match. Verified against the live API:
#
#     BAJAJ-AUTO -> Bajaj Finance          (motorcycles -> NBFC)
#     KEC        -> EPW India              (power infra -> unrelated)
#     HCL-INSYS  -> HCL Technologies       (~Rs 500cr -> ~Rs 4 lakh cr)
#     AERON      -> Hindustan Aeronautics  (small cap -> defence PSU)
#
# It answers HTTP 200 with a well-formed payload, so nothing downstream notices.
# The result is a cache where a ticker carries ANOTHER company's fundamentals -
# far worse than a bad score, because acting on it means trading the wrong stock.
#
# So every response is checked against NSE's own securities master before being
# stored: if the returned company clearly isn't the one we asked for, the record
# is REJECTED rather than silently kept.
_NAME_MATCH_THRESHOLD = 0.66
_NAME_NOISE = {
    "limited", "ltd", "ltd.", "the", "india", "indian", "company", "co",
    "corporation", "corp", "industries", "enterprises", "&", "and",
    "private", "pvt", "public", "plc", "inc", "group", "holdings",
}
_NAME_LEGAL_ONLY = {"limited", "ltd", "ltd.", "the", "private", "pvt", "plc", "inc", "&", "and"}
_nse_master_cache: Optional[dict] = None


def _load_nse_master() -> dict:
    """SYMBOL -> official company name, from NSE's securities master CSV."""
    global _nse_master_cache
    if _nse_master_cache is not None:
        return _nse_master_cache
    out = {}
    path = os.path.join(CACHE_DIR, "EQUITY_L.csv")
    try:
        import csv as _csv
        with open(path, encoding="utf-8", errors="ignore") as f:
            for row in _csv.DictReader(f):
                s = (row.get("SYMBOL") or "").strip().upper()
                n = (row.get("NAME OF COMPANY") or "").strip()
                if s and n:
                    out[s] = n
    except Exception as exc:
        logger.debug("NSE master unavailable for identity check (%s)", exc)
    _nse_master_cache = out
    return out


def _name_clean(name: str, stop: set, expand_amp: bool = False) -> str:
    import re as _re
    s = (name or "").lower()
    if expand_amp:
        s = s.replace("&", " and ")
    s = _re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w and w not in stop)


def _name_similar(cached: str, official: str, symbol: str = "") -> float:
    import re as _re
    from difflib import SequenceMatcher
    # Cached name == the ticker means the API gave no company name and the code
    # fell back to the symbol: missing data, not a wrong company.
    if symbol and _re.sub(r"[^a-z0-9]", "", (cached or "").lower()) == \
                  _re.sub(r"[^a-z0-9]", "", symbol.lower()):
        return 1.0

    na, nb = _name_clean(cached, _NAME_NOISE), _name_clean(official, _NAME_NOISE)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Containment only when the shorter side is >=2 words - one word inside a
    # longer name proves nothing ("lal" is inside "dr lal pathlabs").
    sa, sb = na.split(), nb.split()
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(shorter) >= 2 and " ".join(shorter) in " ".join(longer):
        return 0.95
    # Abbreviation = same company. `&` expanded so M&M matches Mahindra & Mahindra.
    ca, cb = _name_clean(cached, _NAME_LEGAL_ONLY, True), _name_clean(official, _NAME_LEGAL_ONLY, True)
    for a, b in ((ca, cb), (cb, ca)):
        if " " not in a and len(a) >= 2 and a == "".join(w[0] for w in b.split() if w):
            return 0.93
    for a, b in ((ca, cb), (cb, ca)):
        b_init = "".join(w[0] for w in b.split() if w and w != "and")
        a_comp = a.replace(" and ", "").replace(" ", "")
        if len(a_comp) >= 2 and a_comp == b_init:
            return 0.93
    return SequenceMatcher(None, na, nb).ratio()


def _identity_ok(sym: str, returned_name: str) -> bool:
    """True if the returned company plausibly IS the requested ticker.
    Returns True when the master list has no entry (can't verify -> don't block)."""
    official = _load_nse_master().get((sym or "").upper())
    if not official or not returned_name:
        return True
    score = _name_similar(returned_name, official, sym)
    if score < _NAME_MATCH_THRESHOLD:
        logger.warning("IDENTITY MISMATCH for %s: API returned '%s' but NSE master "
                       "says '%s' (similarity %.2f) - rejecting record",
                       sym, returned_name, official, score)
        return False
    return True


def _parse_indianapi(sym: str, data: dict) -> Optional[FundamentalData]:
    """
    Pull fundamentals from IndianAPI's /stock response. The real structure (verified
    against a live response) nests metrics inside keyMetrics.<section>[] as arrays of
    {key, value} objects, and promoter holding inside shareholding[]. We target those
    exact paths, with a fuzzy deep-search fallback for resilience.
    """
    if not isinstance(data, dict) or not data:
        return None

    # Reject a response that is clearly a DIFFERENT company than the one asked
    # for. Better to have no record for a ticker than another company's numbers.
    if not _identity_ok(sym, str(data.get("companyName") or data.get("name") or "")):
        return None

    km = data.get("keyMetrics") or {}

    def from_section(section: str, *wanted_keys):
        """Find a value by key within keyMetrics.<section> (a list of {key,value})."""
        arr = km.get(section)
        if not isinstance(arr, list):
            return None
        # exact-ish match first, then loose contains
        for want in wanted_keys:
            wl = want.lower()
            for item in arr:
                if isinstance(item, dict):
                    k = str(item.get("key", "")).lower().rstrip(")")
                    if k == wl:
                        v = _clean_num(item.get("value"))
                        if v is not None:
                            return v
        for want in wanted_keys:
            wl = want.lower()
            for item in arr:
                if isinstance(item, dict):
                    k = str(item.get("key", "")).lower()
                    if wl in k:
                        v = _clean_num(item.get("value"))
                        if v is not None:
                            return v
        return None

    def dig(*keys):
        """Fallback: recursive fuzzy search anywhere in the payload."""
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    lk = k.lower().replace("_", "").replace(" ", "")
                    if any(n in lk for n in keys) and isinstance(v, (int, float, str)):
                        f = _clean_num(v)
                        if f is not None:
                            return f
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
        return None

    # --- Market cap: keyMetrics.priceandVolume -> marketCap (already in ₹ crore) ---
    mcap = from_section("priceandVolume", "marketCap") or dig("marketcap", "mcap")
    if mcap and mcap > 1e7:        # safety: convert if it ever comes in absolute rupees
        mcap = round(mcap / 1e7, 1)

    # --- P/E: keyMetrics.valuation ---
    pe = from_section("valuation",
                      "pPerEBasicExcludingExtraordinaryItemsTTM",
                      "pPerEIncludingExtraordinaryItemsTTM",
                      "pPerENormalizedMostRecentFiscalYear") or dig("peratio")

    # --- ROE: keyMetrics.mgmtEffectiveness (trailing 12m preferred) ---
    # FIX: this previously had no dig() fallback (unlike mcap/pe/promoter below),
    # so any stock whose payload uses a slightly different key for this metric —
    # common across sectors, e.g. banks/NBFCs often have a different keyMetrics
    # shape than industrials — permanently came back None. Since _is_complete()
    # requires ROE specifically, those stocks could NEVER pass the completeness
    # check and got silently re-fetched (wasting a request) on every single scan,
    # forever, instead of converging into the cache like everything else.
    roe = from_section("mgmtEffectiveness",
                       "returnOnAverageEquityTrailing12Month",
                       "returnOnAverageEquityMostRecentFiscalYear",
                       "returnOnAverageEquity5YearAverage") or dig("returnonequity", "roe")

    # --- Debt/Equity: keyMetrics.financialstrength --- (same fix as ROE above)
    de = from_section("financialstrength",
                      "totalDebtPerTotalEquityMostRecentQuarter",
                      "totalDebtPerTotalEquityMostRecentFiscalYear",
                      "ltDebtPerEquityMostRecentQuarter") or dig("debttoequity", "debtequity")

    # --- Current ratio: keyMetrics.financialstrength ---
    cr = from_section("financialstrength",
                      "currentRatioMostRecentQuarter",
                      "currentRatioMostRecentFiscalYear")

    # --- Growth: keyMetrics.growth ---
    rev_g = from_section("growth",
                         "revenueChangePercentTTMPOverTTM",
                         "revenueGrowthRate5Year",
                         "growthRatePercentRevenue3Year")
    prof_g = from_section("growth",
                          "ePSChangePercentTTMOverTTM",
                          "ePSGrowthRate5Year",
                          "growthRatePercentEPS3year")

    # --- Promoter holding: shareholding[] -> "Promoter" -> latest category % ---
    promoter = None
    sh = data.get("shareholding")
    if isinstance(sh, list):
        for grp in sh:
            if not isinstance(grp, dict):
                continue
            name = (grp.get("displayName") or grp.get("categoryName") or "").lower()
            if "promoter" in name:
                cats = grp.get("categories")
                if isinstance(cats, list) and cats:
                    # pick the most recent by holdingDate
                    latest = max(cats, key=lambda c: str(c.get("holdingDate", "")))
                    promoter = _clean_num(latest.get("percentage"))
                break
    if promoter is None:
        promoter = dig("promoterholding", "promoter")

    # --- Current price: currentPrice.{NSE,BSE} ---
    price = None
    cp = data.get("currentPrice")
    if isinstance(cp, dict):
        price = _clean_num(cp.get("NSE") or cp.get("BSE"))

    # --- Absolute financials + self-computed growth from the real structure ---
    fin = _extract_financials(data)

    fd = FundamentalData(
        symbol=sym,
        name=str(data.get("companyName") or data.get("name") or sym),
        market_cap_cr=mcap or 0.0,
        pe_ratio=pe,
        roe=roe,
        debt_to_equity=de,
        promoter_holding=promoter,
        revenue_growth=rev_g,
        profit_growth=prof_g,
        current_ratio=cr,
        revenue_cr=fin["revenue_cr"],
        net_income_cr=fin["net_income_cr"],
        operating_income_cr=fin["operating_income_cr"],
        fcf_cr=fin["fcf_cr"],
        revenue_growth_calc=fin["revenue_growth_calc"],
        profit_growth_calc=fin["profit_growth_calc"],
        score=0.0,
        last_updated=datetime.now().strftime("%Y-%m-%d"),
    )
    return fd


def _fetch_screener_live() -> List[FundamentalData]:
    """
    Fetch REAL per-stock fundamentals from yfinance for the curated symbol set.

    yfinance's free endpoint rate-limits aggressively (HTTP 429) when hit with a
    burst, so this fetcher is deliberately defensive:
      • A per-stock disk cache (data_cache/fundamentals_by_symbol.json): a symbol
        whose data is still fresh is served from cache and NEVER re-requested, so
        repeated refreshes don't re-hammer Yahoo.
      • A real delay between live requests (SCREENER_FETCH_DELAY_SEC, default 1.5s).
      • Exponential backoff + retry when a 429 is seen.
      • An early-abort: if Yahoo returns 429s repeatedly even after backoff, we stop
        hitting it (returning whatever we have, incl. cached) instead of burning
        through all 76 symbols against a closed door.

    Because of the cache, the FIRST full run is slow (a couple of minutes) but is
    done in chunks across refreshes; later runs are fast. The universe only needs
    refreshing every FUNDAMENTAL_REFRESH_DAYS (default 7) anyway.

    Unit conversions (Yahoo → our model):
      • marketCap: absolute rupees → crores  (÷ 1e7)
      • returnOnEquity / revenueGrowth / earningsGrowth: decimal → percent (× 100)
      • debtToEquity: Yahoo reports it as a percentage (e.g. 50 = 0.5x) → ÷ 100
      • heldPercentInsiders: decimal → percent, PROXY for promoter holding.
    """
    import yfinance as yf

    delay        = float(getattr(settings, "SCREENER_FETCH_DELAY_SEC", 1.5))
    max_429      = int(getattr(settings, "SCREENER_MAX_RATE_LIMIT_HITS", 4))
    cache_days   = int(getattr(settings, "FUNDAMENTAL_REFRESH_DAYS", 7))

    by_symbol    = _load_symbol_cache()          # {symbol: {data..., "_ts": iso}}
    symbols      = [s.symbol for s in _nifty500_fallback()]
    stocks: List[FundamentalData] = []
    fetched = from_cache = failures = 0
    rate_limit_hits = 0

    for sym in symbols:
        # 1) Serve from per-symbol cache if still fresh
        cached = _fresh_cached_symbol(by_symbol, sym, cache_days)
        if cached is not None:
            stocks.append(cached)
            from_cache += 1
            continue

        # 2) If Yahoo has been hard rate-limiting us, stop hitting it
        if rate_limit_hits >= max_429:
            failures += 1
            continue

        info, was_429 = _yf_info_with_retry(yf, _to_yf_symbol(sym), delay)
        if was_429:
            rate_limit_hits += 1
        if not info or not isinstance(info, dict) or not info.get("marketCap"):
            failures += 1
            time.sleep(delay)
            continue

        data = _build_fundamental(sym, info)
        stocks.append(data)
        by_symbol[sym] = {**data.model_dump(), "_ts": datetime.now().isoformat()}
        fetched += 1
        time.sleep(delay)

    _save_symbol_cache(by_symbol)
    if rate_limit_hits >= max_429:
        logger.warning(
            "yfinance rate-limited (429) — stopped early. Fetched %d new, %d from cache, "
            "%d skipped. Yahoo throttles bursts; wait ~15-30 min, then refresh again to "
            "fill in the rest (already-fetched stocks are cached and won't be re-requested).",
            fetched, from_cache, failures)
    else:
        logger.info("yfinance fundamentals: %d new, %d from cache, %d failed/skipped",
                    fetched, from_cache, failures)
    return stocks


def _yf_info_with_retry(yf, yf_sym: str, base_delay: float, retries: int = 2):
    """
    Return (info_dict_or_None, hit_429_bool). Retries with exponential backoff
    when Yahoo returns 429 Too Many Requests.
    """
    hit_429 = False
    for attempt in range(retries + 1):
        try:
            info = yf.Ticker(yf_sym).info
            return info, hit_429
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                hit_429 = True
                wait = base_delay * (2 ** attempt)   # 1.5s, 3s, 6s, ...
                logger.debug("429 for %s — backing off %.1fs (attempt %d)", yf_sym, wait, attempt + 1)
                time.sleep(wait)
                continue
            logger.debug("yfinance info failed for %s: %s", yf_sym, exc)
            return None, hit_429
    return None, hit_429


def _build_fundamental(sym: str, info: dict) -> FundamentalData:
    """Convert a yfinance .info dict into our FundamentalData model (with unit fixes)."""
    mcap_cr = _safe_float(info.get("marketCap"))
    mcap_cr = round(mcap_cr / 1e7, 1) if mcap_cr else 0.0          # rupees → crores
    de_raw  = _safe_float(info.get("debtToEquity"))
    data = FundamentalData(
        symbol=sym,
        name=info.get("shortName") or info.get("longName") or sym,
        market_cap_cr=mcap_cr,
        pe_ratio=_safe_float(info.get("trailingPE")),
        roe=_pct(info.get("returnOnEquity")),
        debt_to_equity=round(de_raw / 100, 2) if de_raw is not None else None,
        promoter_holding=_pct(info.get("heldPercentInsiders")),    # proxy
        revenue_growth=_pct(info.get("revenueGrowth")),
        profit_growth=_pct(info.get("earningsGrowth")),
        current_ratio=_safe_float(info.get("currentRatio")),
        score=0.0,
        last_updated=datetime.now().strftime("%Y-%m-%d"),
    )
    data.score = score_stock(data)
    return data


# ── Per-symbol cache (so repeated refreshes don't re-hit Yahoo) ────────────────
SYMBOL_CACHE_FILE = os.path.join(CACHE_DIR, "fundamentals_by_symbol.json")

# ── Scan lock (prevents two concurrent scans from stomping each other's cache) ─
# CAUSE OF THE "saves then un-saves itself" BUG: uvicorn --reload spins up a
# brand-new worker process (with its own fresh background scan thread) on every
# file save. The OLD worker's scan thread doesn't die instantly — it's a plain
# blocking daemon thread stuck inside requests.get(), so --reload can't interrupt
# it mid-call. For a while you have TWO threads (old process + new process), each
# holding their OWN in-memory snapshot of by_symbol, each periodically
# overwriting the WHOLE file with their own copy. Whichever saves last "wins" —
# even if it's the older/smaller snapshot. That's how progress regresses to
# "297, or anywhere" instead of climbing monotonically. This lock makes a second
# scan back off instead of racing the first one.
LOCK_FILE            = os.path.join(CACHE_DIR, "fundamental_scan.lock")
LOCK_STALE_AFTER_SEC = 60   # no heartbeat for this long => assume the owner died


def _atomic_write_json(path: str, data) -> None:
    """
    Write JSON to `path` without ever leaving a half-written/corrupt file behind.

    The old code did open(path, "w") then json.dump(...) directly. "w" mode
    TRUNCATES the file to 0 bytes first, then streams the new content in. If the
    process is killed/crashes at any point during that write (Ctrl+C lands
    mid-flush, or --reload tears the process down while it's writing), the file
    is left truncated/invalid. The loader then fails to parse it and silently
    returns {} — which looks exactly like "the whole cache vanished", even though
    almost all of the data was fine moments earlier.

    Fix: write to a temp file in the same directory, fsync it, then atomically
    rename it over the real file with os.replace() — atomic on both POSIX and
    Windows. Readers only ever see the fully-old file or the fully-new file,
    never a partial one.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)   # atomic swap, same filesystem
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _try_acquire_scan_lock() -> bool:
    """
    Returns True if it's safe to start scanning now. Returns False if another
    scan looks alive right now (recent heartbeat) — caller should skip starting
    a duplicate and just serve from cache instead of racing it.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        if os.path.exists(LOCK_FILE):
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < LOCK_STALE_AFTER_SEC:
                return False  # another scan touched the lock recently — it's alive
            # else: stale lock (owner crashed/was killed without cleaning up) — take over
    except Exception:
        pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def _touch_scan_lock() -> None:
    """Refresh the heartbeat so a long-running scan never looks stale mid-run."""
    try:
        os.utime(LOCK_FILE, None)
    except Exception:
        pass


def _release_scan_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


def _load_symbol_cache() -> dict:
    try:
        with open(SYMBOL_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# In-memory cache for the (large) per-symbol file, keyed by file mtime. The per-symbol
# cache carries a full _raw blob per stock and can be hundreds of MB once the whole
# universe is enriched; re-reading and re-parsing it on every deep-dive request makes
# the endpoint slow enough to hit the frontend timeout. We parse it once and reuse it
# until the file changes on disk (mtime bump), which happens only on enrich/re-score.
_SYMBOL_CACHE_MEM = {"mtime": None, "data": None}

def _load_symbol_cache_cached() -> dict:
    try:
        mtime = os.path.getmtime(SYMBOL_CACHE_FILE)
    except OSError:
        return {}
    if _SYMBOL_CACHE_MEM["mtime"] != mtime or _SYMBOL_CACHE_MEM["data"] is None:
        try:
            with open(SYMBOL_CACHE_FILE, encoding="utf-8") as f:
                _SYMBOL_CACHE_MEM["data"] = json.load(f)
            _SYMBOL_CACHE_MEM["mtime"] = mtime
        except Exception:
            return {}
    return _SYMBOL_CACHE_MEM["data"]


def _save_symbol_cache(by_symbol: dict) -> None:
    try:
        _atomic_write_json(SYMBOL_CACHE_FILE, by_symbol)
    except Exception as exc:
        logger.debug("symbol cache save failed: %s", exc)


def _recently_checked_empty(by_symbol: dict, sym: str, cooldown_days: int) -> bool:
    """
    True if this symbol was fetched OK but came back with no usable fundamentals
    (roe/de) within the last `cooldown_days`. Phase 1 uses this to skip re-fetching
    stocks the API has already confirmed it has no data for — saving API quota —
    while still allowing a periodic re-check in case coverage is added later.
    """
    rec = by_symbol.get(sym)
    if not rec:
        return False
    stamp = rec.get("_checked_empty")
    if not stamp:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(stamp) <= timedelta(days=cooldown_days)
    except Exception:
        return False


def _is_negative_cached(by_symbol: dict, sym: str, recheck_days: int) -> bool:
    """
    True if this symbol was already fetched successfully but came back WITHOUT
    the screening-critical fundamentals (ROE/debt-equity), and that confirmation
    is recent enough that we shouldn't waste an API request re-checking it yet.

    This is the 'negative cache'. Those ~600 stocks return 200 OK from IndianAPI
    but genuinely contain no ROE/D-E — re-fetching them every scan just burns
    quota for the same empty result. We stamp them with _checked_empty and skip
    them on routine scans. We DON'T ban them forever: after recheck_days the stamp
    is considered stale and the symbol is retried once (in case IndianAPI has since
    added coverage). recheck_days is deliberately much longer than the normal
    cache window since fundamental coverage gaps change slowly.
    """
    rec = by_symbol.get(sym)
    if not rec:
        return False
    stamp = rec.get("_checked_empty")
    if not stamp:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(stamp) <= timedelta(days=recheck_days)
    except Exception:
        return False


def _fresh_cached_symbol(by_symbol: dict, sym: str, cache_days: int):
    """Return a FundamentalData from cache if present and not older than cache_days."""
    rec = by_symbol.get(sym)
    if not rec:
        return None
    ts = rec.get("_ts")
    if ts:
        try:
            if datetime.now() - datetime.fromisoformat(ts) > timedelta(days=cache_days):
                return None   # stale
        except Exception:
            pass
    try:
        # strip internal metadata keys (_ts, _checked_empty, etc.) before building
        clean = {k: v for k, v in rec.items() if not k.startswith("_")}
        return FundamentalData(**clean)
    except Exception:
        return None


def _is_complete(fd) -> bool:
    """
    True if a cached record has the core fundamentals populated. Records produced by
    the earlier buggy parser are 'fresh' but have null roe/debt_to_equity/promoter/
    growth — those count as INCOMPLETE so they get re-fetched and repaired. We require
    the screening-critical fields (ROE, debt/equity, promoter holding); market cap and
    P/E alone (which the old parser did save) are not enough.
    """
    try:
        roe = getattr(fd, "roe", None)
        de  = getattr(fd, "debt_to_equity", None)
        prom = getattr(fd, "promoter_holding", None)
    except Exception:
        return False
    # consider complete only if at least ROE and one of (debt/equity, promoter) exist
    have_roe = roe is not None
    have_other = (de is not None) or (prom is not None)
    return have_roe and have_other


def _to_yf_symbol(symbol: str) -> str:
    """Map our NSE symbol → the yfinance ticker (mirrors data/fetcher.py logic)."""
    s = symbol.upper().replace("NSE:", "").replace("-EQ", "")
    return s + ".NS"


def _pct(v) -> Optional[float]:
    """Yahoo returns ratios as decimals (0.18 = 18%). Convert to percent."""
    f = _safe_float(v)
    return round(f * 100, 2) if f is not None else None


def _parse_screener_result(r: dict) -> FundamentalData:
    name   = r.get("name", "")
    symbol = r.get("symbol", "").replace(" ", "").upper()
    data = FundamentalData(
        symbol=symbol, name=name,
        market_cap_cr=float(r.get("market_cap", 0) or 0),
        pe_ratio=_safe_float(r.get("pe")),
        roe=_safe_float(r.get("roe")),
        debt_to_equity=_safe_float(r.get("debt_to_equity")),
        promoter_holding=_safe_float(r.get("promoter_holding")),
        revenue_growth=_safe_float(r.get("revenue_growth")),
        profit_growth=_safe_float(r.get("profit_growth")),
        current_ratio=_safe_float(r.get("current_ratio")),
        score=0.0,
        last_updated=datetime.now().strftime("%Y-%m-%d"),
    )
    # FIX: score_stock() was fully implemented but never called — every real
    # (non-fallback) stock silently got score=0.0, breaking "Sort by Score"
    # and making every row show red in the universe table.
    data.score = score_stock(data)
    return data


def _passes_filter(s: FundamentalData) -> bool:
    """Return True if stock passes all fundamental filters."""
    if s.market_cap_cr < settings.MIN_MARKET_CAP_CR:
        return False
    # Revenue-scale floor: exclude sub-scale companies from the universe SHORTLIST.
    # Require a REAL revenue figure at/above the floor. Null/zero revenue (dormant
    # shells, data gaps) is excluded from the list — still stored & in Deep Dives.
    min_rev = getattr(settings, "MIN_REVENUE_CR", 0.0)
    rev = getattr(s, "revenue_cr", None)
    if min_rev > 0 and (rev is None or rev < min_rev):
        return False
    if s.roe is not None and s.roe < settings.SCREENER_MIN_ROE:
        return False
    if s.debt_to_equity is not None and s.debt_to_equity > settings.SCREENER_MAX_DE:
        return False
    # NOTE: when fundamentals come from yfinance, promoter_holding is only a rough
    # proxy (heldPercentInsiders), which understates true Indian promoter holding
    # and would wrongly reject good stocks. So the promoter filter is gated behind
    # SCREENER_ENFORCE_PROMOTER (default False). The value is still stored/displayed.
    if getattr(settings, "SCREENER_ENFORCE_PROMOTER", False):
        if s.promoter_holding is not None and s.promoter_holding < settings.SCREENER_MIN_PROMOTER:
            return False
    return True


def _quality_penalty(s: FundamentalData) -> tuple:
    """
    Confidence haircut for stocks whose metrics look elite in isolation but whose
    COMBINATION reveals distortion or dormancy. Returns (multiplier, reasons).

    Why this exists (the MMTC problem): the base score sums six buckets, each of
    which a stock can max out on its own. MMTC — a government trading PSU being
    wound down — scored 99.3/100 by maxing EVERY bucket: its ROE, low debt, and
    promoter holding all look elite, and its profit "growth" of 350% (from a
    one-off NINL asset-sale, not operations) maxed the growth bucket. Capping any
    single bucket does nothing because each was already capped. The only fix that
    works is to detect the distortion in the RELATIONSHIP between metrics and apply
    a multiplicative haircut.

    Works entirely off existing fields (no new data needed):
      1. ONE-OFF PROFIT TELL — profit growth wildly exceeding revenue growth means
         the profit didn't come from selling more (it came from an asset sale,
         a tax writeback, a base effect, etc.). Real operating quality shows
         profit and revenue growing roughly together.
      2. DORMANCY TELL — zero debt + stagnant/negative revenue growth is an idle
         balance sheet, not financial strength. A debt-free company that's still
         GROWING revenue is untouched; one that's debt-free because it stopped
         doing business gets penalised.
    """
    rg = s.revenue_growth
    pg = s.profit_growth
    de = s.debt_to_equity
    mult = 1.0
    reasons = []

    # 1. One-off profit tell — graduated by HOW EXTREME the divergence is. MMTC's
    #    profit grew ~13x faster than revenue (350% vs 27%), a clear sign the profit
    #    came from a one-off (asset sale) not operations; it gets hammered. A company
    #    whose profit merely grew a bit faster than sales is barely touched. This is
    #    deliberately harsh at the extreme end so wind-down shells sink toward the
    #    bottom of the ranking, not merely the middle.
    if pg is not None and pg > 50:
        if rg is None or rg <= 0:
            mult *= 0.25; reasons.append("profit growth with no revenue growth (one-off)")
        elif pg > rg * 8:
            mult *= 0.30; reasons.append("profit growth >8x revenue growth (one-off / asset sale)")
        elif pg > rg * 4:
            mult *= 0.50; reasons.append("profit growth >4x revenue growth (likely one-off)")
        elif pg > rg * 2.5:
            mult *= 0.75; reasons.append("profit growth >2.5x revenue growth")

    # 2. Dormancy tell: zero debt + weak/negative revenue growth = idle balance
    #    sheet, not financial strength. A debt-free company still GROWING is untouched.
    if de == 0 and (rg is None or rg < 5):
        mult *= 0.60; reasons.append("zero debt with stagnant revenue (possible dormancy)")

    return mult, reasons


def score_stock(s: FundamentalData) -> float:
    """Score 0-100 based on fundamentals, with a quality haircut for distorted profiles."""
    score = 0.0
    # ROE (max 25 pts)
    if s.roe is not None:
        score += min(25, s.roe * 1.2)
    # Revenue growth (max 20 pts)
    if s.revenue_growth is not None and s.revenue_growth > 0:
        score += min(20, s.revenue_growth)
    # Profit growth (max 20 pts)
    if s.profit_growth is not None and s.profit_growth > 0:
        score += min(20, s.profit_growth)
    # Low debt (max 15 pts)
    if s.debt_to_equity is not None:
        score += max(0, 15 - s.debt_to_equity * 5)
    # Promoter holding (max 10 pts)
    if s.promoter_holding is not None:
        score += min(10, (s.promoter_holding - 40) * 0.2)
    # PE reasonable (max 10 pts)
    if s.pe_ratio is not None and 5 < s.pe_ratio < 40:
        score += 10
    # Quality haircut: penalise distorted/dormant profiles (e.g. MMTC) where the
    # COMBINATION of metrics reveals one-off profit or a dormant balance sheet.
    mult, _reasons = _quality_penalty(s)
    score *= mult
    return round(min(100, score), 1)


def get_approved_symbols() -> List[str]:
    """Return list of NSE symbols that pass fundamental filter."""
    universe = load_universe()
    return [s.symbol for s in universe]


def _safe_float(v) -> Optional[float]:
    try: return float(v) if v is not None else None
    except: return None


def _clean_num(v) -> Optional[float]:
    """
    Parse a number that may arrive as a messy string from a fundamentals API,
    e.g. '18.2%', '1,234.5', '₹50,000', '- ' (dash for N/A), '12.3 Cr'.
    Returns None if there's no real number.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s in ("-", "--", "N/A", "NA", "null", "None"):
        return None
    # keep digits, sign, decimal point; drop %, commas, ₹, letters, spaces
    cleaned = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    # guard against stray multiple dots/dashes
    if cleaned in ("", "-", ".", "-.", ".-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _nifty500_fallback() -> List[FundamentalData]:
    """Curated list of NIFTY 500 stocks as fallback when Screener is unavailable."""
    nifty500 = [
        "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","BAJFINANCE","TITAN","ITC",
        "WIPRO","SBIN","AXISBANK","KOTAKBANK","LT","HINDUNILVR","ASIANPAINT",
        "MARUTI","SUNPHARMA","TATAMOTORS","TATASTEEL","NTPC","POWERGRID","ONGC",
        "COALINDIA","JSWSTEEL","M&M","ULTRACEMCO","BHARTIARTL","ADANIENT","ADANIPORTS",
        "NESTLEIND","BAJAJFINSV","GRASIM","HCLTECH","TECHM","DIVISLAB","DRREDDY",
        "CIPLA","EICHERMOT","HEROMOTOCO","BAJAJ-AUTO","BRITANNIA","TATACONSUM",
        "PIDILITIND","HAVELLS","VOLTAS","PERSISTENT","LTIM","MPHASIS","COFORGE",
        "ZOMATO","PAYTM","NYKAA","DMART","IRCTC","IEX","TATAPOWER","TORNTPHARM",
        "AUROPHARMA","LUPIN","BIOCON","ALKEM","IPCALAB","NATCOPHARM","GRANULES",
        "HDFC","BAJAJHLDNG","CHOLAFIN","SHRIRAMFIN","MUTHOOTFIN","MANAPPURAM",
        "FEDERALBNK","IDFCFIRSTB","INDUSINDBNK","BANDHANBNK","AUBANK","DCBBANK",
    ]
    stocks = []
    for sym in nifty500:
        data = FundamentalData(
            symbol=sym, name=sym, market_cap_cr=5000.0,
            roe=15.0, debt_to_equity=0.5, promoter_holding=50.0,
            revenue_growth=15.0, profit_growth=15.0, score=0.0,
            last_updated=datetime.now().strftime("%Y-%m-%d"),
        )
        data.score = score_stock(data)  # FIX: compute consistently instead of hardcoding 60.0
        stocks.append(data)
    return stocks


def deep_dive(symbol: str) -> Optional[dict]:
    """
    Return EVERYTHING stored for one stock for the Stock Deep Dives dashboard:
    the extracted fields, the six-category score breakdown, and a set of
    chart-ready series derived from the stored raw IndianAPI response.

    Reads only from the per-symbol cache (no network). Returns None if the symbol
    isn't cached. Built so the frontend can render without re-deriving anything.
    """
    by_symbol = _load_symbol_cache_cached()
    sym = symbol.upper().replace("NSE:", "").replace("-EQ", "").strip()
    rec = by_symbol.get(sym)
    if rec is None:
        # loose match (case / suffix differences)
        for k, v in by_symbol.items():
            if k.upper() == sym:
                rec, sym = v, k
                break
    if rec is None:
        return None

    raw = rec.get("_raw") or {}
    breakdown = rec.get("_breakdown")

    # ----- chart-ready series from the raw financials[] (annual, oldest→newest) -----
    annual_rows = []
    for blk in (raw.get("financials") or []):
        if not isinstance(blk, dict) or blk.get("Type") != "Annual":
            continue
        fy = str(blk.get("FiscalYear") or "")
        fmap = blk.get("stockFinancialMap") or {}
        inc = {str(i.get("key", "")).lower(): _clean_num(i.get("value"))
               for i in (fmap.get("INC") or []) if isinstance(i, dict)}
        bal = {str(i.get("key", "")).lower(): _clean_num(i.get("value"))
               for i in (fmap.get("BAL") or []) if isinstance(i, dict)}
        cas = {str(i.get("key", "")).lower(): _clean_num(i.get("value"))
               for i in (fmap.get("CAS") or []) if isinstance(i, dict)}
        if not fy:
            continue
        annual_rows.append({
            "year": fy,
            "revenue": inc.get("revenue") or inc.get("totalrevenue"),
            "net_income": inc.get("netincome"),
            "operating_income": inc.get("operatingincome"),
            "gross_profit": inc.get("grossprofit"),
            "total_debt": bal.get("totaldebt"),
            "total_equity": bal.get("totalequity"),
            "operating_cash_flow": cas.get("cashfromoperatingactivities"),
            "capex": cas.get("capitalexpenditures"),
        })
    annual_rows.sort(key=lambda r: r["year"])
    # de-dup by year (raw has both Annual and some repeats)
    seen, annual = set(), []
    for r in annual_rows:
        if r["year"] not in seen:
            seen.add(r["year"]); annual.append(r)

    # ----- shareholding series (for pie + trend) -----
    shareholding = []
    for grp in (raw.get("shareholding") or []):
        if not isinstance(grp, dict):
            continue
        cats = [c for c in (grp.get("categories") or []) if isinstance(c, dict)]
        cats.sort(key=lambda c: str(c.get("holdingDate", "")))
        latest = _clean_num(cats[-1].get("percentage")) if cats else None
        shareholding.append({
            "category": grp.get("displayName") or grp.get("categoryName"),
            "latest": latest,
            "series": [{"date": c.get("holdingDate"),
                        "pct": _clean_num(c.get("percentage"))} for c in cats],
        })

    # ----- analyst view (context, not scored) -----
    analyst = []
    for a in (raw.get("analystView") or []):
        if isinstance(a, dict) and a.get("ratingName") != "Total":
            analyst.append({"rating": a.get("ratingName"),
                            "count": _clean_num(a.get("numberOfAnalystsLatest"))})

    # ----- peers (coerce numerics so the frontend never gets strings) -----
    peers = []
    for p in (raw.get("peerCompanyList") or [])[:8]:
        if isinstance(p, dict):
            peers.append({
                "name": p.get("companyName"),
                "pe": _clean_num(p.get("priceToEarningsValueRatio")),
                "pb": _clean_num(p.get("priceToBookValueRatio")),
                "roe": _clean_num(p.get("returnOnAverageEquityTrailing12Month")),
                "market_cap_cr": _clean_num(p.get("marketCap")),
            })

    # ----- price context for the header (percent change, 52-week range) -----
    price_ctx = {}
    for it in ((raw.get("keyMetrics") or {}).get("priceandVolume") or []):
        if isinstance(it, dict):
            k = str(it.get("key", ""))
            if k == "price1DayPercentChange":
                price_ctx["change_1d_pct"] = _clean_num(it.get("value"))
            elif k == "52WeekHigh":
                price_ctx["week52_high"] = _clean_num(it.get("value"))
            elif k == "52WeekLow":
                price_ctx["week52_low"] = _clean_num(it.get("value"))
            elif k == "priceYTDPricePercentChange":
                price_ctx["change_ytd_pct"] = _clean_num(it.get("value"))

    return {
        "symbol": sym,
        "name": rec.get("name"),
        "industry": raw.get("industry") or raw.get("mgIndustry"),
        "current_price": (raw.get("currentPrice") or {}),
        "price_ctx": price_ctx,
        "score": rec.get("score"),
        "extracted": {k: rec.get(k) for k in (
            "market_cap_cr", "pe_ratio", "roe", "debt_to_equity", "promoter_holding",
            "revenue_cr", "net_income_cr", "operating_income_cr", "fcf_cr",
            "revenue_growth_calc", "profit_growth_calc", "current_ratio")},
        "breakdown": breakdown,
        "annual": annual,
        "shareholding": shareholding,
        "analyst": analyst,
        "peers": peers,
        "flags": {k: rec.get(k) for k in ("_unit_mismatch", "_fcf_suspect") if rec.get(k)},
        "company_description": (raw.get("companyProfile") or {}).get("companyDescription"),
        "has_raw": bool(raw),
    }
