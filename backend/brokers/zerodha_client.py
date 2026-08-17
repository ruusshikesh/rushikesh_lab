"""
Zerodha (Kite Connect) client - ORDERS (existing) + HISTORICAL DATA (new).

ORDERS / AUTH (unchanged, used by main.py):
    ZerodhaClient.get_login_url()      -> str
    ZerodhaClient.exchange_token(rt)   -> str
    ZerodhaClient.place_order(order)   -> str
    ZerodhaClient.quote(symbol)        -> dict

DATA (new - used by data/fetcher.py when DATA_PROVIDER="zerodha"):
    KiteData.fetch_ohlcv(symbol, timeframe, start, end, days) -> pd.DataFrame
    KiteData.get_quote(symbol)                                -> dict

Requires the PAID Kite Connect plan (Rs 500/mo) for historical data - the free
"Personal" API does NOT include historical candles.

Requires:  pip install kiteconnect
Config (.env): ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_ACCESS_TOKEN

Daily auth (token expires daily). Fully-guided CLI:
    python -m brokers.zerodha_client auth
  Opens the login page, you log in, copy the request_token from the redirected
  URL, paste it, and it writes ZERODHA_ACCESS_TOKEN into .env automatically.

VERIFIED Kite historical per-request limits (from kite.trade docs):
    1min=60d, 3/5/10min=100d, 15/30min=200d, 60min=400d, 1day=2000d.
  Minute data is backfilled to ~2015. Longer ranges are fetched by LOOPING
  windows (done automatically below). Historical API is rate-limited to ~3 req/s,
  so we throttle between windows.
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import pytz

from config import settings

logger = logging.getLogger(__name__)
IST    = pytz.timezone(settings.TZ)


# ---------------------------------------------------------------------------
# PROCESS-WIDE rate limiter for the historical API
# ---------------------------------------------------------------------------
# Kite's historical endpoint allows roughly 3 requests/second. The previous
# throttle was a bare time.sleep(0.34) inside each call's own window loop, which
# only paces ONE thread - it places no cap on the TOTAL when several threads call
# concurrently. radar.py fetches with 8 workers, so the real rate was ~24 req/s,
# about 8x the limit, on EVERY radar run (measured, not estimated). That produces
# 429s, retry storms, and a slow radar that looks like a data problem.
#
# This limiter is module-level and lock-guarded, so it caps the rate across every
# thread in the process rather than per call. Same pattern already used for
# Finnhub in data_us/fundamental_us.py - the Zerodha path simply never got one.
class _KiteRateLimiter:
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
                wait = self.period - (now - self._calls[0]) + 0.01
            time.sleep(max(wait, 0.01))


# 2 rather than 3: leaves headroom so a retry can't tip us over the edge.
_kite_hist_limiter = _KiteRateLimiter(max_calls=2, period_sec=1.0)


def _kite(with_token: bool = True):
    """Build a KiteConnect client. Imported lazily so the app still starts if the
    kiteconnect package isn't installed (only order/auth/data paths need it)."""
    try:
        from kiteconnect import KiteConnect
    except ImportError as e:
        raise RuntimeError(
            "kiteconnect not installed. Run:  pip install kiteconnect"
        ) from e
    kc = KiteConnect(api_key=settings.ZERODHA_API_KEY)
    if with_token:
        if not settings.ZERODHA_ACCESS_TOKEN:
            raise RuntimeError(
                "ZERODHA_ACCESS_TOKEN is empty - complete the daily login "
                "(python -m brokers.zerodha_client auth) and set the token first."
            )
        kc.set_access_token(settings.ZERODHA_ACCESS_TOKEN)
    return kc


class ZerodhaClient:

    # -- auth ------------------------------------------------------------------
    @staticmethod
    def get_login_url() -> str:
        return _kite(with_token=False).login_url()

    @staticmethod
    def exchange_token(request_token: str) -> str:
        if not request_token:
            raise ValueError("request_token is empty")
        kc = _kite(with_token=False)
        data = kc.generate_session(request_token,
                                   api_secret=settings.ZERODHA_API_SECRET)
        token = data["access_token"]
        logger.info("Zerodha session established; access_token acquired.")
        return token

    # -- order placement -------------------------------------------------------
    @staticmethod
    def place_order(order) -> str:
        """
        Place a REGULAR order on Zerodha. `order` is the Order pydantic model
        (schemas.Order). Returns the broker order id.

        NOTE: this places the ENTRY order. Stop-loss / target are attached
        separately by the caller (OCO/GTT or bracket) - kept out of here so this
        method stays a single, testable responsibility.
        """
        kc = _kite(with_token=True)
        from kiteconnect import KiteConnect

        side = "BUY" if str(order.side).upper() == "BUY" else "SELL"
        otype = "MARKET" if str(order.order_type).upper() == "MARKET" else "LIMIT"

        # intraday (MIS) vs delivery (CNC) from the order's trade_type
        product = "MIS"
        try:
            tt = str(getattr(order, "trade_type", "intraday")).lower()
            product = "CNC" if "deliver" in tt or "cnc" in tt else "MIS"
        except Exception:
            pass

        params = dict(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=KiteConnect.EXCHANGE_NSE,
            tradingsymbol=order.symbol.upper(),
            transaction_type=side,
            quantity=int(order.qty),
            product=product,
            order_type=otype,
        )
        if otype == "LIMIT":
            params["price"] = float(order.price)

        logger.info("Zerodha place_order: %s", params)
        oid = kc.place_order(**params)
        logger.info("Zerodha order placed, id=%s", oid)
        return str(oid)

    # -- optional: live quote --------------------------------------------------
    @staticmethod
    def quote(symbol: str) -> dict:
        kc = _kite(with_token=True)
        key = f"NSE:{symbol.upper()}"
        q = kc.quote([key])
        return q.get(key, {})


# ==============================================================================
# DATA SIDE - historical OHLCV + quotes for the backtester / scanner.
# ==============================================================================
class KiteData:
    """Zerodha historical OHLCV + quotes. Shares the same access_token as orders."""

    _instruments: Optional[Dict[str, int]] = None   # tradingsymbol -> instrument_token

    # our timeframe -> (kite interval string, max days per single request)
    _RESOLUTION = {
        "1min":  ("minute",     60),
        "3min":  ("3minute",   100),
        "5min":  ("5minute",   100),
        "10min": ("10minute",  100),
        "15min": ("15minute",  200),
        "30min": ("30minute",  200),
        "1hr":   ("60minute",  400),
        "1day":  ("day",      2000),
    }

    _INSTR_CACHE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data_cache", "kite_instruments_nse.json",
    )

    # -- instrument master (symbol -> token) -----------------------------------
    @classmethod
    def _load_instruments(cls) -> Dict[str, int]:
        if cls._instruments is not None:
            return cls._instruments

        # fresh-enough disk cache (refresh weekly)
        mapping: Dict[str, int] = {}
        try:
            if (os.path.exists(cls._INSTR_CACHE)
                    and (time.time() - os.path.getmtime(cls._INSTR_CACHE)) < 7 * 86400):
                with open(cls._INSTR_CACHE, encoding="utf-8") as f:
                    mapping = {k: int(v) for k, v in json.load(f).items()}
        except Exception:
            mapping = {}

        if not mapping:
            kc = _kite(with_token=True)
            rows = kc.instruments("NSE")          # large list of dicts
            for r in rows:
                sym   = str(r.get("tradingsymbol", "")).upper().strip()
                tok   = r.get("instrument_token")
                itype = str(r.get("instrument_type", "")).upper()
                if sym and tok and itype == "EQ":   # NSE equities only
                    mapping[sym] = int(tok)
            try:
                os.makedirs(os.path.dirname(cls._INSTR_CACHE), exist_ok=True)
                with open(cls._INSTR_CACHE, "w", encoding="utf-8") as f:
                    json.dump(mapping, f)
            except Exception as exc:
                logger.debug("Kite instrument cache write failed: %s", exc)
            logger.info("Kite instruments loaded - %d NSE equities", len(mapping))

        cls._instruments = mapping
        return mapping

    @classmethod
    def _token(cls, symbol: str) -> int:
        s = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
        from data.excluded_symbols import is_excluded, exclusion_reason
        if is_excluded(s):
            raise ValueError(f"Kite: {s} is excluded - {exclusion_reason(s)}")
        tok = cls._load_instruments().get(s)
        if not tok:
            raise ValueError(
                f"Kite: no instrument_token for {s}. Not an NSE equity, or the "
                f"instrument cache is stale - delete data_cache/kite_instruments_nse.json "
                f"to force a refresh."
            )
        return int(tok)

    # -- historical OHLCV (chunked + throttled) --------------------------------
    @classmethod
    def fetch_ohlcv(cls, symbol, timeframe, start=None, end=None, days=60) -> pd.DataFrame:
        interval, max_days = cls._RESOLUTION.get(timeframe, cls._RESOLUTION["5min"])
        token = cls._token(symbol)
        kc    = _kite(with_token=True)

        end_dt   = datetime.now(IST) if not end   else datetime.fromisoformat(end).replace(tzinfo=IST)
        start_dt = (end_dt - timedelta(days=days)) if not start else datetime.fromisoformat(start).replace(tzinfo=IST)

        # Chunk range into <= max_days windows (Kite rejects oversized ranges).
        step = timedelta(days=max(1, max_days - 1))
        windows, cur = [], start_dt
        while cur < end_dt:
            w_end = min(cur + step, end_dt)
            windows.append((cur, w_end))
            cur = w_end + timedelta(days=1)
        if not windows:
            windows = [(start_dt, end_dt)]

        frames = []
        for w_start, w_end in windows:
            for attempt in range(3):
                _kite_hist_limiter.acquire()   # global cap, safe under concurrency
                try:
                    candles = kc.historical_data(
                        token,
                        w_start.strftime("%Y-%m-%d %H:%M:%S"),
                        w_end.strftime("%Y-%m-%d %H:%M:%S"),
                        interval,
                    )
                    if candles:
                        frames.append(pd.DataFrame(candles))
                    break
                except Exception as exc:
                    msg = str(exc).lower()
                    # Back off on transient throttle/network; otherwise surface it.
                    if any(k in msg for k in ("too many", "rate", "timeout", "network", "connection")):
                        time.sleep(1 + attempt)
                        continue
                    if frames:
                        logger.warning("Kite window %s-%s failed: %s",
                                       w_start.date(), w_end.date(), exc)
                        break
                    raise ValueError(f"Kite history error for {symbol}: {exc}")
            # No per-thread sleep here any more - the limiter above enforces the
            # cap globally. A local sleep would slow a single fetch down while
            # still doing nothing about the concurrent case.

        if not frames:
            raise ValueError(f"Kite returned no candles for {symbol} "
                             f"({start_dt.date()}-{end_dt.date()})")

        df = pd.concat(frames, ignore_index=True)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].dropna(subset=["close"])
        df = df[~df.index.duplicated(keep="first")]
        return df.sort_index()

    # -- live quote (REST) -----------------------------------------------------
    @classmethod
    def get_quote(cls, symbol) -> dict:
        kc  = _kite(with_token=True)
        s   = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
        key = f"NSE:{s}"
        q   = (kc.quote([key]) or {}).get(key, {})
        if not q:
            raise ValueError(f"Kite quote: no data for {symbol}")
        ltp  = float(q.get("last_price") or 0)
        ohlc = q.get("ohlc") or {}
        prev = float(ohlc.get("close") or ltp or 0)
        return {"symbol": symbol, "ltp": ltp,
                "open": float(ohlc.get("open") or ltp), "high": float(ohlc.get("high") or ltp),
                "low": float(ohlc.get("low") or ltp), "close": prev,
                "change_pct": round((ltp - prev) / prev * 100, 2) if prev else 0,
                "timestamp": datetime.now(IST).isoformat()}


# -- CLI: python -m brokers.zerodha_client auth --------------------------------
# Manual paste flow (no server): prints the login URL, opens the browser, you log
# in, copy the request_token from the redirected URL, paste it, and it writes
# ZERODHA_ACCESS_TOKEN into .env automatically.
if __name__ == "__main__":
    import sys
    import webbrowser

    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    if cmd != "auth":
        print(f"Unknown command: {cmd}. Use: python -m brokers.zerodha_client auth")
        sys.exit(1)

    if not settings.ZERODHA_API_KEY or not settings.ZERODHA_API_SECRET:
        print("Set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env first.")
        sys.exit(1)

    url = ZerodhaClient.get_login_url()
    print("\n" + "=" * 70)
    print("STEP 1 - a browser will open. Log in to Zerodha (Kite).")
    print("If it doesn't open, paste this URL manually:")
    print(url)
    print("=" * 70)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("\nSTEP 2 - after login the browser redirects to a URL like:")
    print("  https://127.0.0.1/?action=login&status=success&request_token=XXXXXXXX")
    print("(The page may show a connection error - that's fine, the token is in")
    print(" the address bar.) Copy the request_token value and paste it below.\n")

    request_token = input("Paste request_token here: ").strip()
    if not request_token:
        print("No request_token entered. Aborting.")
        sys.exit(1)

    print("\nExchanging for access token...")
    token = ZerodhaClient.exchange_token(request_token)

    # Write / update ZERODHA_ACCESS_TOKEN in .env automatically
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    lines, found = [], False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("ZERODHA_ACCESS_TOKEN"):
            lines[i] = f"ZERODHA_ACCESS_TOKEN={token}\n"
            found = True
            break
    if not found:
        lines.append(f"ZERODHA_ACCESS_TOKEN={token}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print("\n" + "=" * 70)
    print("SUCCESS - token written to .env automatically.")
    print("=" * 70)
    print("Set DATA_PROVIDER=zerodha in .env to use Kite for backtest data,")
    print("then restart the backend:  uvicorn main:app --port 8000\n")
