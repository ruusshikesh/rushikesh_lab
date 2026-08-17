"""
Angel One SmartAPI client — DATA source (historical OHLCV + live quotes).

Key benefit over Fyers: login is fully automatic via TOTP — no daily browser
dance, no copy-pasting auth codes. As long as ANGEL_TOTP_SECRET is set, this
logs in by itself each time the token is needed.

Requires:  pip install smartapi-python pyotp logzero websocket-client
Credentials (in .env):
  ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import requests

from config import settings

logger = logging.getLogger(__name__)

# Angel's public instrument master (symbol -> token). No auth required.
ANGEL_SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_SCRIP_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data_cache", "angel_scrip_master.json",
)

# Angel resolution codes + their max days-per-request (Angel enforces these).
# Map our app timeframes → (angel_interval, max_days_per_request).
_RESOLUTION = {
    "1min":  ("ONE_MINUTE",      30),
    "3min":  ("THREE_MINUTE",    60),
    "5min":  ("FIVE_MINUTE",    100),
    "15min": ("FIFTEEN_MINUTE", 200),
    "30min": ("THIRTY_MINUTE",  200),
    "1hr":   ("ONE_HOUR",       400),
    "1day":  ("ONE_DAY",       2000),   # ~5.5 yrs/request; we chunk anyway
}


class AngelClient:
    _token_map: Optional[Dict[str, str]] = None   # symbol -> instrument token
    _session = None                               # SmartConnect instance
    _session_ts: float = 0.0                      # when we last logged in

    # ── Auto-login (TOTP) ────────────────────────────────────────────────────
    @classmethod
    def _connect(cls):
        """Return a logged-in SmartConnect, re-using the session for ~6 hours."""
        # Re-use existing session if it's fresh (Angel sessions last the day,
        # but we refresh every 6h to be safe).
        if cls._session is not None and (time.time() - cls._session_ts) < 6 * 3600:
            return cls._session

        if not all([settings.ANGEL_API_KEY, settings.ANGEL_CLIENT_ID,
                    settings.ANGEL_PIN, settings.ANGEL_TOTP_SECRET]):
            raise ValueError("Angel One not configured — set ANGEL_API_KEY, "
                             "ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET in .env")

        from SmartApi import SmartConnect          # smartapi-python
        import pyotp

        otp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
        sc  = SmartConnect(api_key=settings.ANGEL_API_KEY)
        data = sc.generateSession(settings.ANGEL_CLIENT_ID, settings.ANGEL_PIN, otp)
        if not data or not data.get("status"):
            raise ValueError(f"Angel login failed: {data}")
        cls._session    = sc
        cls._session_ts = time.time()
        logger.info("Angel One logged in (auto-TOTP) as %s", settings.ANGEL_CLIENT_ID)
        return sc

    # ── Instrument master (symbol -> token) ──────────────────────────────────
    @classmethod
    def _load_token_map(cls) -> Dict[str, str]:
        if cls._token_map is not None:
            return cls._token_map
        raw = cls._read_scrip()
        mapping: Dict[str, str] = {}
        try:
            for row in json.loads(raw):
                # NSE equity rows: exch_seg == "NSE", symbol ends with "-EQ"
                if row.get("exch_seg") != "NSE":
                    continue
                sym = (row.get("symbol") or "").upper()
                name = (row.get("name") or "").upper()
                if sym.endswith("-EQ"):
                    base = sym[:-3]
                    if base and row.get("token"):
                        mapping[base] = row["token"]
        except Exception as exc:
            logger.error("Angel scrip parse failed: %s", exc)
        cls._token_map = mapping
        logger.info("Angel instrument master loaded — %d NSE symbols", len(mapping))
        return mapping

    @staticmethod
    def _read_scrip() -> str:
        try:
            if os.path.exists(_SCRIP_CACHE) and os.path.getsize(_SCRIP_CACHE) > 100_000:
                # refresh weekly
                age = time.time() - os.path.getmtime(_SCRIP_CACHE)
                if age < 7 * 86400:
                    with open(_SCRIP_CACHE, encoding="utf-8") as f:
                        return f.read()
        except Exception:
            pass
        resp = requests.get(ANGEL_SCRIP_URL, timeout=60)
        resp.raise_for_status()
        try:
            os.makedirs(os.path.dirname(_SCRIP_CACHE), exist_ok=True)
            with open(_SCRIP_CACHE, "w", encoding="utf-8") as f:
                f.write(resp.text)
        except Exception as exc:
            logger.debug("Angel scrip cache write failed: %s", exc)
        return resp.text

    @classmethod
    def _token(cls, symbol: str) -> str:
        sym = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
        tok = cls._load_token_map().get(sym)
        if not tok:
            raise ValueError(f"Angel: no instrument token for {sym}")
        return tok

    # ── Historical OHLCV (chunked to respect per-resolution limits) ───────────
    @classmethod
    def fetch_ohlcv(cls, symbol, timeframe, start, end, days) -> pd.DataFrame:
        from config import settings as _s
        sc = cls._connect()
        interval, max_days = _RESOLUTION.get(timeframe, _RESOLUTION["5min"])
        token = cls._token(symbol)

        end_dt   = datetime.now() if not end   else datetime.fromisoformat(end)
        start_dt = (end_dt - timedelta(days=days)) if not start else datetime.fromisoformat(start)

        # Chunk the range into <= max_days windows (Angel rejects oversized ranges).
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
            params = {
                "exchange": "NSE", "symboltoken": token, "interval": interval,
                "fromdate": w_start.strftime("%Y-%m-%d 09:15"),
                "todate":   w_end.strftime("%Y-%m-%d 15:30"),
            }
            try:
                r = sc.getCandleData(params)
            except Exception as exc:
                if frames:
                    logger.warning("Angel candle window %s–%s failed: %s",
                                   w_start.date(), w_end.date(), exc)
                    continue
                raise
            # FIX: Angel signals errors via {"status": False, "message": ...} WITHOUT
            # raising. Surface that real message instead of silently treating it as
            # "no candles" (which hid rate-limit / bad-token errors). Mirror the Fyers fix.
            if isinstance(r, dict) and r.get("status") is False:
                msg = r.get("message") or r.get("errorcode") or r
                # If we already have data from earlier windows, keep it; else raise clearly.
                if frames:
                    logger.warning("Angel candle window %s–%s error: %s",
                                   w_start.date(), w_end.date(), msg)
                    continue
                raise ValueError(f"Angel history error for {symbol}: {msg}")
            candles = (r or {}).get("data") or []
            if candles:
                frames.append(pd.DataFrame(
                    candles, columns=["timestamp", "open", "high", "low", "close", "volume"]))
            time.sleep(0.35)   # Angel rate-limit courtesy

        if not frames:
            raise ValueError(f"Angel returned no candles for {symbol} "
                             f"({start_dt.date()}–{end_dt.date()})")
        df = pd.concat(frames, ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        # FIX: Angel sometimes returns OHLCV as strings. Force numeric so the
        # indicator library (RSI/EMA/etc.) doesn't crash or misbehave on string data.
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[~df.index.duplicated(keep="first")]
        return df.sort_index()

    # ── Live quote ────────────────────────────────────────────────────────────
    @classmethod
    def get_quote(cls, symbol) -> dict:
        sc = cls._connect()
        token = cls._token(symbol)
        clean = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "")
        r = sc.ltpData("NSE", f"{clean}-EQ", token)
        # FIX: surface Angel's structured error instead of silently returning zeros.
        if isinstance(r, dict) and r.get("status") is False:
            raise ValueError(f"Angel quote error for {symbol}: {r.get('message') or r.get('errorcode') or r}")
        d = (r or {}).get("data") or {}
        if not d:
            raise ValueError(f"Angel quote: no data for {symbol}: {r}")
        ltp  = float(d.get("ltp") or 0)
        prev = float(d.get("close") or ltp or 0)
        return {"symbol": symbol, "ltp": ltp,
                "open": float(d.get("open") or ltp), "high": float(d.get("high") or ltp),
                "low": float(d.get("low") or ltp), "close": prev,
                "change_pct": round((ltp - prev) / prev * 100, 2) if prev else 0,
                "timestamp": datetime.now().isoformat()}
