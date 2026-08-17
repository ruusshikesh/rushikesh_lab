"""
Rush Algo - Buy & Sell Radar
============================
Screens the EXISTING fundamentally-approved universe (data.fundamental) for
mean-reversion setups:

  BUY RADAR  = deep pullback + fundamental strength
    - "fallen" score: how far below its recent high the price has dropped
    - blended with the existing fundamental score (0-100)
    - deeper fall + stronger fundamentals -> higher Buy %

  SELL RADAR = strong bounce + fundamental strength
    - "bounced" score: how far above its recent low the price has recovered
    - blended with fundamental score
    - stronger bounce + stronger fundamentals -> higher Sell %

This is a SCREENER/RANKING, not an auto-trade signal - it surfaces candidates
for you to review, it does not place orders. Uses daily OHLC (Zerodha) over a
lookback window, so it reflects swing-level moves, not intraday noise.

DAILY MEMORY: every computation is saved as a dated snapshot under
  data_cache/radar_history/YYYY-MM-DD.json
so you build a running history of what the radar flagged each day - useful
for later review or backtesting the radar's own call quality.

USAGE:
  from radar import compute_radar
  buy_list, sell_list = compute_radar(top_n=25)
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_HISTORY_DIR = os.path.join(_BACKEND_DIR, "data_cache", "radar_history")
_LATEST_FILE = os.path.join(_HISTORY_DIR, "latest.json")

# Lookback window (calendar days) for computing recent high/low on daily bars.
LOOKBACK_DAYS = 60

# Blend weights: technical (fall/bounce) vs fundamental score. Tune here.
W_TECHNICAL = 0.6
W_FUNDAMENTAL = 0.4

# Only consider stocks whose fundamental score clears this bar (quality floor).
# Keeps the radar to "strong companies having a technical moment", not junk.
MIN_FUNDAMENTAL_SCORE = 55.0

# Cap on how much "fall" or "bounce" keeps adding score - beyond this, further
# fall/bounce is treated as equally extreme (avoids one crashed/pumped stock
# dominating purely on magnitude with weak fundamentals dragging it up anyway;
# MIN_FUNDAMENTAL_SCORE already filters those, this is a secondary safety).
MAX_MOVE_PCT_FOR_SCORE = 25.0


def _ensure_dirs():
    os.makedirs(_HISTORY_DIR, exist_ok=True)


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _daily_high_low(symbol: str) -> Optional[Tuple[float, float, float]]:
    """Return (recent_high, recent_low, last_close) over LOOKBACK_DAYS of daily
    bars, or None if data isn't available. Uses the existing data fetcher, so
    it automatically goes through Zerodha (with Fyers/yfinance fallback)."""
    try:
        from data.fetcher import fetch_ohlcv
        df = fetch_ohlcv(symbol, timeframe="1day", days=LOOKBACK_DAYS)
        if df is None or df.empty or len(df) < 5:
            return None
        recent_high = float(df["high"].max())
        recent_low  = float(df["low"].min())
        last_close  = float(df["close"].iloc[-1])
        return recent_high, recent_low, last_close
    except Exception as exc:
        logger.debug("radar: no OHLC for %s: %s", symbol, exc)
        return None


def _norm(pct: float) -> float:
    """Clamp+scale a 0..MAX_MOVE_PCT_FOR_SCORE move into a 0..100 technical score."""
    pct = max(0.0, min(pct, MAX_MOVE_PCT_FOR_SCORE))
    return round(pct / MAX_MOVE_PCT_FOR_SCORE * 100, 1)


# In-memory + on-disk cache of (recent_high, recent_low, last_close) per symbol
# for TODAY, so re-clicking Refresh doesn't re-fetch OHLC for 2500+ stocks each
# time - only stocks not yet fetched today hit the network again.
_HL_CACHE_FILE = os.path.join(_HISTORY_DIR, "_hl_cache_today.json")
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


def _load_hl_cache():
    global _hl_cache, _hl_cache_date
    today = date.today().isoformat()
    if _hl_cache_date == today and _hl_cache:
        return
    _hl_cache = {}
    try:
        if os.path.exists(_HL_CACHE_FILE):
            with open(_HL_CACHE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("date") == today:
                _hl_cache = saved.get("data", {})
    except Exception:
        pass
    _hl_cache_date = today


def _save_hl_cache():
    _ensure_dirs()
    try:
        _atomic_write_json(_HL_CACHE_FILE, {"date": _hl_cache_date, "data": _hl_cache})
    except Exception as exc:
        logger.debug("radar: hl cache write failed: %s", exc)


def compute_radar(top_n: int = 25, universe: Optional[List] = None, max_workers: int = 8) -> Tuple[List[dict], List[dict]]:
    """
    Compute today's Buy Radar and Sell Radar from the fundamental universe.
    Returns (buy_list, sell_list), each a list of dicts sorted best-first,
    and also persists a dated snapshot to data_cache/radar_history/.

    Runs OHLC fetches CONCURRENTLY (thread pool) and caches each symbol's
    high/low/close for the day, so repeated calls (e.g. a manual Refresh)
    only fetch symbols not already resolved today - not the whole universe
    every time. Without this, ~2500 sequential network calls can take 15-40+
    minutes; with concurrency + caching, subsequent calls the same day are
    near-instant for already-fetched symbols.
    """
    # Refuse to run a second computation concurrently - return today's snapshot
    # instead. Blocking here would hold an HTTP request open for minutes.
    if not _compute_lock.acquire(blocking=False):
        logger.info("radar: computation already in progress - returning last snapshot")
        snap = load_snapshot() or {}
        return snap.get("buy", []), snap.get("sell", [])
    try:
        if universe is None:
            from data.fundamental import approved_from_cache
            universe = approved_from_cache()

        _load_hl_cache()

        qualifying = [fd for fd in universe
                      if getattr(fd, "score", None) is not None
                      and fd.score >= MIN_FUNDAMENTAL_SCORE]

        to_fetch = [fd for fd in qualifying if fd.symbol not in _hl_cache]
        if to_fetch:
            logger.info("Radar: fetching OHLC for %d/%d symbols (rest cached from today)",
                        len(to_fetch), len(qualifying))
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_daily_high_low, fd.symbol): fd.symbol for fd in to_fetch}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    try:
                        hl = fut.result()
                        if hl:
                            _hl_cache[sym] = {"high": hl[0], "low": hl[1], "close": hl[2]}
                    except Exception as exc:
                        logger.debug("radar: fetch failed for %s: %s", sym, exc)
            _save_hl_cache()

        buy_candidates: List[dict] = []
        sell_candidates: List[dict] = []

        for fd in qualifying:
            fscore = fd.score
            cached = _hl_cache.get(fd.symbol)
            if not cached:
                continue
            recent_high, recent_low, last_close = cached["high"], cached["low"], cached["close"]
            if recent_high <= 0 or recent_low <= 0 or last_close <= 0:
                continue

            # How far BELOW the recent high (drawdown) -> buy-the-dip candidate.
            fall_pct = max(0.0, (recent_high - last_close) / recent_high * 100)
            # How far ABOVE the recent low (recovery) -> take-profit candidate.
            bounce_pct = max(0.0, (last_close - recent_low) / recent_low * 100)

            fund_component = round(fscore, 1)   # already 0-100

            # MUTUAL EXCLUSIVITY: a stock sitting anywhere between its high and low
            # technically satisfies BOTH "fallen from high" and "bounced off low" at
            # once (e.g. price 90, high 100, low 80 -> 10% fall AND 12.5% bounce are
            # both simultaneously true). Without a tie-breaker a stock can rank #1 on
            # Buy and also appear on Sell, which is confusing and not what "buy vs
            # sell" is supposed to mean. Fix: only the HALF OF ITS RANGE the price is
            # actually in decides which list it's eligible for -
            #   below the midpoint (closer to the low) -> Buy candidate only
            #   above the midpoint (closer to the high) -> Sell candidate only
            midpoint = (recent_high + recent_low) / 2
            in_lower_half = last_close <= midpoint

            if in_lower_half and fall_pct > 0.5:   # ignore near-flat noise
                tech = _norm(fall_pct)
                buy_score = round(W_TECHNICAL * tech + W_FUNDAMENTAL * fund_component, 1)
                buy_candidates.append({
                    "symbol": fd.symbol, "name": getattr(fd, "name", fd.symbol),
                    "buy_score": buy_score,
                    "fall_pct": round(fall_pct, 2),
                    "fundamental_score": fund_component,
                    "last_close": round(last_close, 2),
                    "recent_high": round(recent_high, 2),
                    "recent_low": round(recent_low, 2),
                })

            elif (not in_lower_half) and bounce_pct > 0.5:
                tech = _norm(bounce_pct)
                sell_score = round(W_TECHNICAL * tech + W_FUNDAMENTAL * fund_component, 1)
                sell_candidates.append({
                    "symbol": fd.symbol, "name": getattr(fd, "name", fd.symbol),
                    "sell_score": sell_score,
                    "bounce_pct": round(bounce_pct, 2),
                    "fundamental_score": fund_component,
                    "last_close": round(last_close, 2),
                    "recent_high": round(recent_high, 2),
                    "recent_low": round(recent_low, 2),
                })

        buy_candidates.sort(key=lambda x: x["buy_score"], reverse=True)
        sell_candidates.sort(key=lambda x: x["sell_score"], reverse=True)
        buy_list = buy_candidates[:top_n]
        sell_list = sell_candidates[:top_n]

        _save_snapshot(buy_list, sell_list)
        return buy_list, sell_list


    finally:
        _compute_lock.release()

def _save_snapshot(buy_list: List[dict], sell_list: List[dict]) -> None:
    """Persist today's radar as dated memory + update the 'latest' pointer.
    Safe/atomic writes - never leaves a half-written file behind."""
    _ensure_dirs()
    today = date.today().isoformat()
    snapshot = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "buy": buy_list,
        "sell": sell_list,
        "params": {
            "lookback_days": LOOKBACK_DAYS,
            "w_technical": W_TECHNICAL,
            "w_fundamental": W_FUNDAMENTAL,
            "min_fundamental_score": MIN_FUNDAMENTAL_SCORE,
        },
    }
    dated_path = os.path.join(_HISTORY_DIR, f"{today}.json")
    _atomic_write_json(dated_path, snapshot)
    _atomic_write_json(_LATEST_FILE, snapshot)
    logger.info("Radar snapshot saved: %d buy / %d sell -> %s", len(buy_list), len(sell_list), dated_path)


def load_snapshot(for_date: Optional[str] = None) -> Optional[dict]:
    """Load a saved radar snapshot. for_date='YYYY-MM-DD' or None for latest."""
    path = _LATEST_FILE if not for_date else os.path.join(_HISTORY_DIR, f"{for_date}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("radar: failed to load snapshot %s: %s", path, exc)
        return None


def list_available_dates() -> List[str]:
    """List all dates that have a saved radar snapshot (for a history dropdown)."""
    _ensure_dirs()
    out = []
    for fn in os.listdir(_HISTORY_DIR):
        if fn.endswith(".json") and fn != "latest.json":
            out.append(fn[:-5])
    return sorted(out, reverse=True)
