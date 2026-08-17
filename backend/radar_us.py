"""
Rush Algo - US Buy & Sell Radar
==================================
Mirrors radar.py's logic exactly, INCLUDING the mutual-exclusivity fix
(midpoint rule) already applied to the NSE version - built in from the start
here rather than repeating the bug we found and fixed in NSE radar.

Fully separate from radar.py - own cache, own compute function, own snapshot
history. A bug here cannot affect NSE radar and vice versa.
"""
from __future__ import annotations
import json
import logging
import os
import threading
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_HISTORY_DIR = os.path.join(_BACKEND_DIR, "data_cache_us", "radar_history_us")
_LATEST_FILE = os.path.join(_HISTORY_DIR, "latest.json")

LOOKBACK_DAYS = 60
W_TECHNICAL = 0.6
W_FUNDAMENTAL = 0.4
MIN_FUNDAMENTAL_SCORE = 45.0   # US engine is simpler/stricter scale than NSE's - lower floor
MAX_MOVE_PCT_FOR_SCORE = 25.0

_hl_cache: Dict[str, dict] = {}

# Serialises radar computation. TWO callers can trigger it: the hourly
# _bg_radar_daily thread in main.py, and a user clicking Refresh
# (/api/radar?refresh=true). With no guard they overlap and both spawn their own
# worker pool, both mutate the shared _hl_cache, and both write the SAME snapshot
# file - so one full run's API spend is silently discarded. Non-blocking: a
# second caller returns the existing snapshot instead of queueing behind a
# multi-minute job and holding an HTTP request open.
_compute_lock = threading.Lock()
_hl_cache_date: Optional[str] = None


def _ensure_dirs():
    os.makedirs(_HISTORY_DIR, exist_ok=True)


def _atomic_write_json(path: str, data) -> None:
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _norm(pct: float) -> float:
    pct = max(0.0, min(pct, MAX_MOVE_PCT_FOR_SCORE))
    return round(pct / MAX_MOVE_PCT_FOR_SCORE * 100, 1)


def _daily_high_low_us(symbol: str) -> Optional[Tuple[float, float, float]]:
    try:
        from data_us.fetcher_us import fetch_ohlcv_us
        df = fetch_ohlcv_us(symbol, timeframe="1day", days=LOOKBACK_DAYS)
        if df is None or df.empty or len(df) < 5:
            return None
        return float(df["high"].max()), float(df["low"].min()), float(df["close"].iloc[-1])
    except Exception as exc:
        logger.debug("US radar: no OHLC for %s: %s", symbol, exc)
        return None


def compute_radar_us(top_n: int = 25, universe: Optional[List] = None, max_workers: int = 8) -> Tuple[List[dict], List[dict]]:
    """Same design as compute_radar() (NSE): concurrent OHLC fetch + daily
    cache so repeated calls don't re-fetch, mutual-exclusivity via midpoint
    rule so a stock is only ever Buy OR Sell, never both."""
    # Refuse to run a second computation concurrently - return today's snapshot
    # instead. Blocking here would hold an HTTP request open for minutes.
    if not _compute_lock.acquire(blocking=False):
        logger.info("radar: computation already in progress - returning last snapshot")
        snap = load_snapshot_us() or {}
        return snap.get("buy", []), snap.get("sell", [])
    try:
        global _hl_cache, _hl_cache_date
        today = date.today().isoformat()
        if _hl_cache_date != today:
            _hl_cache = {}
            _hl_cache_date = today

        if universe is None:
            from data_us.fundamental_us import approved_from_cache_us
            universe = approved_from_cache_us()

        qualifying = [fd for fd in universe
                      if getattr(fd, "score", None) is not None
                      and fd.score >= MIN_FUNDAMENTAL_SCORE]

        to_fetch = [fd for fd in qualifying if fd.symbol not in _hl_cache]
        if to_fetch:
            logger.info("US radar: fetching OHLC for %d/%d symbols", len(to_fetch), len(qualifying))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_daily_high_low_us, fd.symbol): fd.symbol for fd in to_fetch}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    try:
                        hl = fut.result()
                        if hl:
                            _hl_cache[sym] = {"high": hl[0], "low": hl[1], "close": hl[2]}
                    except Exception as exc:
                        logger.debug("US radar fetch failed for %s: %s", sym, exc)

        buy_candidates: List[dict] = []
        sell_candidates: List[dict] = []

        for fd in qualifying:
            cached = _hl_cache.get(fd.symbol)
            if not cached:
                continue
            recent_high, recent_low, last_close = cached["high"], cached["low"], cached["close"]
            if recent_high <= 0 or recent_low <= 0 or last_close <= 0:
                continue

            fall_pct = max(0.0, (recent_high - last_close) / recent_high * 100)
            bounce_pct = max(0.0, (last_close - recent_low) / recent_low * 100)
            fund_component = round(fd.score, 1)

            # Mutual exclusivity via midpoint - see radar.py for the full rationale.
            midpoint = (recent_high + recent_low) / 2
            in_lower_half = last_close <= midpoint

            if in_lower_half and fall_pct > 0.5:
                tech = _norm(fall_pct)
                buy_score = round(W_TECHNICAL * tech + W_FUNDAMENTAL * fund_component, 1)
                buy_candidates.append({
                    "symbol": fd.symbol, "name": getattr(fd, "name", fd.symbol),
                    "buy_score": buy_score, "fall_pct": round(fall_pct, 2),
                    "fundamental_score": fund_component,
                    "last_close": round(last_close, 2),
                    "recent_high": round(recent_high, 2), "recent_low": round(recent_low, 2),
                })
            elif (not in_lower_half) and bounce_pct > 0.5:
                tech = _norm(bounce_pct)
                sell_score = round(W_TECHNICAL * tech + W_FUNDAMENTAL * fund_component, 1)
                sell_candidates.append({
                    "symbol": fd.symbol, "name": getattr(fd, "name", fd.symbol),
                    "sell_score": sell_score, "bounce_pct": round(bounce_pct, 2),
                    "fundamental_score": fund_component,
                    "last_close": round(last_close, 2),
                    "recent_high": round(recent_high, 2), "recent_low": round(recent_low, 2),
                })

        buy_candidates.sort(key=lambda x: x["buy_score"], reverse=True)
        sell_candidates.sort(key=lambda x: x["sell_score"], reverse=True)
        buy_list = buy_candidates[:top_n]
        sell_list = sell_candidates[:top_n]
        _save_snapshot_us(buy_list, sell_list)
        return buy_list, sell_list


    finally:
        _compute_lock.release()

def _save_snapshot_us(buy_list: List[dict], sell_list: List[dict]) -> None:
    _ensure_dirs()
    today = date.today().isoformat()
    snapshot = {
        "date": today, "generated_at": datetime.now().isoformat(),
        "buy": buy_list, "sell": sell_list,
        "params": {"lookback_days": LOOKBACK_DAYS, "w_technical": W_TECHNICAL,
                   "w_fundamental": W_FUNDAMENTAL, "min_fundamental_score": MIN_FUNDAMENTAL_SCORE},
    }
    _atomic_write_json(os.path.join(_HISTORY_DIR, f"{today}.json"), snapshot)
    _atomic_write_json(_LATEST_FILE, snapshot)
    logger.info("US radar snapshot saved: %d buy / %d sell", len(buy_list), len(sell_list))


def load_snapshot_us(for_date: Optional[str] = None) -> Optional[dict]:
    path = _LATEST_FILE if not for_date else os.path.join(_HISTORY_DIR, f"{for_date}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("US radar snapshot load failed: %s", exc)
        return None
