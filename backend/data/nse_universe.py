"""
NSE equity master list — the full list of tradeable NSE equities.

IndianAPI has no "list all stocks" endpoint, so we get the universe of symbols
from NSE's official EQUITY_L.csv (free, public). The COUNT is whatever the file
contains — never hardcoded — so it naturally tracks NSE's actual listings
(currently ~2000, but this code does not assume any number).

Strategy (robust to NSE's anti-bot blocking):
  1. Use a locally-provided CSV if present (data_cache/EQUITY_L.csv or an
     uploaded copy). This is the reliable path — NSE often blocks scripts.
  2. Otherwise try to download it with browser-like headers + a warm-up cookie.
  3. Cache whatever we get to disk so we never depend on NSE being reachable twice.

EQUITY_L.csv columns (stable):
  SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
  MARKET LOT, ISIN NUMBER, FACE VALUE
We keep only SERIES == "EQ" (regular equity) by default.
"""
from __future__ import annotations
import csv
import io
import logging
import os
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")
_CACHE_CSV = os.path.join(_CACHE_DIR, "EQUITY_L.csv")

# Places we'll look for a user-provided copy (so a manual download "just works").
_LOCAL_CANDIDATES = [
    _CACHE_CSV,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "EQUITY_L.csv"),
    "/mnt/user-data/uploads/EQUITY_L.csv",
]

_NSE_URLS = [
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://www1.nseindia.com/content/equities/EQUITY_L.csv",
]


def get_nse_symbols(series_filter: Tuple[str, ...] = ("EQ",)) -> List[str]:
    """
    Return the list of NSE equity symbols. Count = whatever the CSV holds.
    Tries local file → cache → live download, in that order.
    """
    raw = _read_local() or _download_nse() or _read_cache()
    if not raw:
        logger.warning("NSE master list unavailable (no local file, download blocked, "
                       "no cache). Universe will fall back to the curated seed.")
        return []

    symbols = _parse(raw, series_filter)
    logger.info("NSE master list: %d symbols (series=%s)", len(symbols), ",".join(series_filter))
    return symbols


def _read_local() -> Optional[str]:
    for path in _LOCAL_CANDIDATES:
        try:
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                if "SYMBOL" in txt[:200].upper():
                    logger.info("NSE master list: using local file %s", path)
                    return txt
        except Exception as exc:
            logger.debug("NSE local read failed for %s: %s", path, exc)
    return None


def _download_nse() -> Optional[str]:
    """Try to download with browser-like headers. NSE often blocks scripts; that's OK."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "text/csv,application/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for url in _NSE_URLS:
        try:
            sess = requests.Session()
            sess.headers.update(headers)
            # warm-up hit to pick up cookies (NSE checks these)
            try:
                sess.get("https://www.nseindia.com", timeout=10)
            except Exception:
                pass
            r = sess.get(url, timeout=20)
            if r.status_code == 200 and "SYMBOL" in r.text[:200].upper():
                _write_cache(r.text)
                logger.info("NSE master list: downloaded from %s", url)
                return r.text
            logger.debug("NSE download %s returned %s", url, r.status_code)
        except Exception as exc:
            logger.debug("NSE download failed (%s): %s", url, exc)
    return None


def _read_cache() -> Optional[str]:
    try:
        if os.path.exists(_CACHE_CSV) and os.path.getsize(_CACHE_CSV) > 1000:
            with open(_CACHE_CSV, encoding="utf-8", errors="ignore") as f:
                logger.info("NSE master list: using cached copy")
                return f.read()
    except Exception:
        pass
    return None


def _write_cache(text: str) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_CSV, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        logger.debug("NSE cache write failed: %s", exc)


def _parse(raw: str, series_filter: Tuple[str, ...]) -> List[str]:
    """Parse EQUITY_L.csv → list of symbols. Column names matched fuzzily/by header."""
    out: List[str] = []
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return out
    # find the symbol and series columns regardless of stray spaces in headers
    def col(*needles):
        for c in reader.fieldnames:
            k = c.strip().upper().replace(" ", "")
            if all(n in k for n in needles):
                return c
        return None
    c_sym = col("SYMBOL")
    c_ser = col("SERIES")
    if not c_sym:
        return out
    want = {s.upper() for s in series_filter}
    for row in reader:
        sym = (row.get(c_sym, "") or "").strip().upper()
        if not sym:
            continue
        if c_ser and want:
            ser = (row.get(c_ser, "") or "").strip().upper()
            if ser and ser not in want:
                continue
        out.append(sym)
    # de-dup preserving order
    seen = set()
    return [s for s in out if not (s in seen or seen.add(s))]
