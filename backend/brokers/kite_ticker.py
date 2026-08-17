"""
Zerodha KiteTicker live streaming service.
==========================================
One persistent WebSocket to Zerodha that streams live LTP for whatever symbols
are currently on screen. Runs in a background thread alongside FastAPI.

USED BY:
  POST /api/live/subscribe  {symbols:[...]}  -> set what to stream (on-screen rows)
  GET  /api/live/ticks?symbols=a,b,c         -> latest streamed prices from memory

DESIGN:
  - Real WebSocket (KiteTicker), NOT repeated REST quote calls.
  - MODE_LTP subscription (lightest) - we only need the live price.
  - In-memory {symbol: {ltp, prev_close, change_pct, ts}} updated on every tick.
  - Dynamic subscription: as the frontend scrolls, it sends the visible symbols;
    the service subscribes new tokens and unsubscribes ones no longer shown
    (max ~3000 tokens on one connection, but we only ever stream what's visible).
  - Market CLOSED: the socket connects but no ticks arrive, so memory stays empty
    for price -> the ticks endpoint fills prev_close via a cached REST quote so the
    UI shows LAST CLOSE until the market opens and live ticks take over.

Requires: kiteconnect (already installed), valid ZERODHA_ACCESS_TOKEN in .env.
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class KiteTickerService:
    def __init__(self):
        self._ticker = None
        self._lock = threading.Lock()
        self._ticks: Dict[str, dict] = {}          # symbol -> {ltp, prev_close, change_pct, ts, live}
        self._token_to_sym: Dict[int, str] = {}     # instrument_token -> symbol
        self._sym_to_token: Dict[str, int] = {}     # symbol -> instrument_token
        self._subscribed: set = set()               # instrument_tokens currently subscribed
        self._want: set = set()                      # instrument_tokens we WANT subscribed (union)
        self._client_wants: Dict[str, set] = {}      # client_id -> its own wanted tokens
        self._connected = False
        self._started = False
        self._prev_close_cache: Dict[str, dict] = {} # symbol -> last-close quote (REST fallback)
        self._prev_close_ts: float = 0.0

        # Index instruments aren't in the EQ instrument dump. Well-known Zerodha
        # index tokens (stable). Kite quote key for these is "NSE:NIFTY 50" etc.
        self._index_tokens = {
            "NIFTY": 256265, "NIFTY50": 256265, "NIFTY 50": 256265,
            "BANKNIFTY": 260105, "NIFTY BANK": 260105,
            "FINNIFTY": 257801, "NIFTY FIN SERVICE": 257801,
        }
        self._index_quote_key = {
            256265: "NSE:NIFTY 50", 260105: "NSE:NIFTY BANK", 257801: "NSE:NIFTY FIN SERVICE",
        }

    # -- lifecycle -------------------------------------------------------------
    def _ensure_started(self):
        """Lazily start the KiteTicker connection on first use."""
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            if not settings.ZERODHA_API_KEY or not settings.ZERODHA_ACCESS_TOKEN:
                raise RuntimeError("Zerodha not configured (API key / access token).")
            try:
                from kiteconnect import KiteTicker
            except ImportError as e:
                raise RuntimeError("kiteconnect not installed.") from e

            kt = KiteTicker(settings.ZERODHA_API_KEY, settings.ZERODHA_ACCESS_TOKEN)
            kt.on_ticks     = self._on_ticks
            kt.on_connect   = self._on_connect
            kt.on_close     = self._on_close
            kt.on_error     = self._on_error
            kt.on_reconnect = self._on_reconnect
            self._ticker = kt
            # threaded=True runs the socket in its own thread; doesn't block FastAPI.
            kt.connect(threaded=True)
            self._started = True
            logger.info("KiteTicker: connection starting (threaded).")

    # -- KiteTicker callbacks --------------------------------------------------
    def _on_connect(self, ws, response):
        self._connected = True
        logger.info("KiteTicker: connected.")
        self._resync()

    def _on_close(self, ws, code, reason):
        self._connected = False
        logger.warning("KiteTicker: closed (%s %s)", code, reason)

    def _on_error(self, ws, code, reason):
        logger.warning("KiteTicker: error (%s %s)", code, reason)

    def _on_reconnect(self, ws, attempts):
        logger.info("KiteTicker: reconnecting (attempt %s)", attempts)

    def _on_ticks(self, ws, ticks):
        now = time.time()
        with self._lock:
            for t in ticks:
                tok = t.get("instrument_token")
                sym = self._token_to_sym.get(tok)
                if not sym:
                    continue
                ltp = t.get("last_price")
                # LTP mode has no ohlc; prev close comes from the REST fallback cache.
                prev = (t.get("ohlc") or {}).get("close")
                if prev is None:
                    prev = self._prev_close_cache.get(sym, {}).get("prev_close")
                chg = None
                if ltp is not None and prev:
                    chg = round((ltp - prev) / prev * 100, 2)
                self._ticks[sym] = {
                    "symbol": sym, "ltp": ltp, "prev_close": prev,
                    "change_pct": chg, "ts": now, "live": True,
                }

    # -- subscription management ------------------------------------------------
    def _resync(self):
        """Make the socket's subscription match self._want."""
        if not (self._ticker and self._connected):
            return
        try:
            from kiteconnect import KiteTicker
            want = set(self._want)
            add = list(want - self._subscribed)
            rem = list(self._subscribed - want)
            if add:
                self._ticker.subscribe(add)
                self._ticker.set_mode(KiteTicker.MODE_LTP, add)
            if rem:
                self._ticker.unsubscribe(rem)
            self._subscribed = want
        except Exception as exc:
            logger.warning("KiteTicker resync failed: %s", exc)

    def set_symbols(self, symbols: List[str], client: str = "default") -> int:
        """Set the symbols this CLIENT wants streamed. Returns count resolved.

        FIX (real bug): this used to do `self._want = want_tokens`, REPLACING the
        entire subscription list on every call. Different parts of the UI
        subscribe independently - the NIFTY header asks for ["NIFTY"], the
        Universe panel asks for its visible rows - so whichever called last
        silently unsubscribed the other. In practice the Universe panel (which
        re-subscribes on every scroll) kept wiping NIFTY, so the NIFTY ticker
        froze at its last value.

        Now each caller has its own `client` slot, and the actual subscription
        is the UNION of all slots. A panel changing its rows no longer knocks
        another panel's symbols off the stream."""
        self._ensure_started()
        from brokers.zerodha_client import KiteData
        instr = KiteData._load_instruments()

        want_tokens = set()
        with self._lock:
            for raw in symbols:
                s = raw.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
                tok = self._index_tokens.get(s) or instr.get(s)
                if tok:
                    want_tokens.add(tok)
                    self._token_to_sym[tok] = s
                    self._sym_to_token[s] = tok
            # store per-client, then union across all clients
            self._client_wants[client] = want_tokens
            self._want = set().union(*self._client_wants.values()) if self._client_wants else set()
            # Seed prices instantly for any symbol we don't already have a tick
            # for. Read self._ticks INSIDE the lock - it's written concurrently
            # by the WebSocket thread in _on_ticks, so reading it unlocked could
            # throw "dictionary changed size during iteration".
            # Seed anything we don't have a FRESH tick for. Checking mere
            # presence in self._ticks isn't enough: a tick written before a
            # disconnect (overnight, token expiry, market close) lingers in
            # memory forever, so a stale entry would suppress the seed and the
            # UI would keep showing an ancient price - exactly the frozen-NIFTY
            # failure. Apply the same staleness rule get_ticks() uses.
            _now = time.time()
            need_seed = [
                s for s in (self._token_to_sym.get(t) for t in want_tokens)
                if s and (
                    s not in self._ticks
                    or self._ticks[s].get("ltp") is None
                    or (_now - self._ticks[s].get("ts", 0)) > self.TICK_STALE_SEC
                )
            ]
        self._resync()
        if need_seed:
            try:
                self._last_close(need_seed)
            except Exception:
                pass
        return len(want_tokens)

    # -- reads -----------------------------------------------------------------
    # A cached tick older than this is treated as STALE and refetched via REST.
    # Without this, a tick written before a disconnect (overnight, token expiry,
    # market close) sits in memory forever still flagged live:true, and every
    # request happily serves that ancient price - which is exactly what made the
    # NIFTY header freeze on a >24h-old value while looking "live".
    TICK_STALE_SEC = 60

    def get_ticks(self, symbols: List[str]) -> Dict[str, dict]:
        """Return latest prices for the requested symbols. Uses the live tick if
        it's recent; if the cached tick is older than TICK_STALE_SEC it's treated
        as stale and refreshed from the REST quote instead (and marked
        live: false so the UI can label it honestly as a close, not a live tick)."""
        syms = [s.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
                for s in symbols]
        now = time.time()
        out: Dict[str, dict] = {}
        missing = []
        with self._lock:
            for s in syms:
                cached = self._ticks.get(s)
                if (cached and cached.get("ltp") is not None
                        and (now - cached.get("ts", 0)) <= self.TICK_STALE_SEC):
                    out[s] = cached
                else:
                    # stale or absent -> drop it so a fresh REST quote is fetched
                    if cached:
                        self._ticks.pop(s, None)
                    missing.append(s)

        if missing:
            fallback = self._last_close(missing)
            for s in missing:
                if s in fallback:
                    out[s] = fallback[s]
        return out

    def _last_close(self, symbols: List[str]) -> Dict[str, dict]:
        """Batched REST quote for last close (fallback when no live tick yet).
        Cached ~10s to avoid hammering the quote API."""
        now = time.time()
        # Only fetch symbols we don't have, or whose cached close is stale (>60s).
        need = [s for s in symbols
                if s not in self._prev_close_cache
                or (now - self._prev_close_cache[s].get("ts", 0)) > 60]
        if need:
            try:
                from brokers.zerodha_client import _kite
                kc = _kite(with_token=True)
                # index symbols need their special quote key ("NSE:NIFTY 50")
                idx_key_by_sym = {}
                keys = []
                for s in need:
                    tok = self._index_tokens.get(s)
                    if tok and tok in self._index_quote_key:
                        k = self._index_quote_key[tok]
                        idx_key_by_sym[k] = s
                        keys.append(k)
                    else:
                        keys.append(f"NSE:{s}")
                # Kite quote accepts up to 500 instruments per call
                for i in range(0, len(keys), 400):
                    chunk = keys[i:i + 400]
                    q = kc.quote(chunk) or {}
                    for key, data in q.items():
                        s = idx_key_by_sym.get(key, key.replace("NSE:", ""))
                        ltp = float(data.get("last_price") or 0)
                        prev = float((data.get("ohlc") or {}).get("close") or ltp or 0)
                        self._prev_close_cache[s] = {
                            "symbol": s, "ltp": ltp, "prev_close": prev,
                            "change_pct": round((ltp - prev) / prev * 100, 2) if prev else None,
                            "ts": now, "live": False,
                        }
                self._prev_close_ts = now
            except Exception as exc:
                logger.debug("last-close fallback failed: %s", exc)
        return {s: self._prev_close_cache[s] for s in symbols if s in self._prev_close_cache}

    def status(self) -> dict:
        return {"started": self._started, "connected": self._connected,
                "subscribed": len(self._subscribed), "cached_ticks": len(self._ticks)}


# module-level singleton
ticker_service = KiteTickerService()
