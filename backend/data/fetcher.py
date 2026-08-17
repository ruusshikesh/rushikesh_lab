"""Rush Algo — Data Fetcher with fallback tickers for NSE indices"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from config import settings

logger = logging.getLogger(__name__)
IST    = pytz.timezone(settings.TZ)

# Primary ticker → yfinance symbol
NSE_SYMBOLS: dict = {
    "NIFTY":"^NSEI","BANKNIFTY":"^NSEBANK","FINNIFTY":"NIFTY_FIN_SERVICE.NS",
    "RELIANCE":"RELIANCE.NS","TCS":"TCS.NS","INFY":"INFY.NS",
    "HDFCBANK":"HDFCBANK.NS","ICICIBANK":"ICICIBANK.NS","BAJFINANCE":"BAJFINANCE.NS",
    "TITAN":"TITAN.NS","ITC":"ITC.NS","WIPRO":"WIPRO.NS","SBIN":"SBIN.NS",
    "AXISBANK":"AXISBANK.NS","KOTAKBANK":"KOTAKBANK.NS","LT":"LT.NS",
    "HINDUNILVR":"HINDUNILVR.NS","ASIANPAINT":"ASIANPAINT.NS","MARUTI":"MARUTI.NS",
    "SUNPHARMA":"SUNPHARMA.NS","IEX":"IEX.NS","TATAMOTORS":"TATAMOTORS.NS",
    "TATASTEEL":"TATASTEEL.NS","NTPC":"NTPC.NS","POWERGRID":"POWERGRID.NS",
    "ONGC":"ONGC.NS","COALINDIA":"COALINDIA.NS","JSWSTEEL":"JSWSTEEL.NS",
    "M&M":"M&M.NS","ULTRACEMCO":"ULTRACEMCO.NS","BHARTIARTL":"BHARTIARTL.NS",
    "ADANIENT":"ADANIENT.NS","ADANIPORTS":"ADANIPORTS.NS",
}

# Fallback tickers tried in order when primary fails
FALLBACKS: dict = {
    "^NSEI":    ["^NSEI","NIFTYBEES.NS"],
    "^NSEBANK": ["^NSEBANK","BANKBEES.NS"],
}

TF_TO_YF: dict = {
    "1min":"1m","3min":"2m","5min":"5m","15min":"15m",
    "30min":"30m","1hr":"60m","1day":"1d",
}
YF_MAX_DAYS: dict = {
    "1m":7,"2m":60,"5m":60,"15m":60,"30m":60,"60m":730,"1d":3650,
}


def _to_yf(symbol: str) -> str:
    s = symbol.upper().replace("NSE:","").replace("-EQ","").replace("-INDEX","")
    return NSE_SYMBOLS.get(s, s+".NS")


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    return df


def _download(primary: str, start: str, end: str, interval: str) -> pd.DataFrame:
    """
    Download with fallback tickers + retry with exponential backoff.

    Yahoo rate-limits bursts and, when throttled, can return either an empty frame
    OR a misleading error (e.g. YFTzMissingError 'possibly delisted; no timezone
    found'). Neither means the symbol is actually bad — so we retry with growing
    waits before giving up, which lets a short throttle clear on its own instead
    of failing the whole backtest on the first hiccup.
    """
    tickers   = FALLBACKS.get(primary, [primary])
    max_tries = 4
    saw_rate_limit = False
    for ticker in tickers:
        for attempt in range(max_tries):
            try:
                df = yf.download(ticker, start=start, end=end,
                                 interval=interval, progress=False, auto_adjust=True)
                if not df.empty:
                    if ticker != primary:
                        logger.info("Used fallback ticker %s for %s", ticker, primary)
                    return df
                # empty frame — likely throttled; back off and retry
                if attempt < max_tries - 1:
                    wait = 2 ** attempt   # 1s, 2s, 4s
                    logger.debug("Empty data for %s (attempt %d) — retrying in %ds", ticker, attempt + 1, wait)
                    time.sleep(wait)
            except Exception as exc:
                msg = str(exc)
                if "429" in msg or "Too Many Requests" in msg or "Timezone" in msg or "delisted" in msg:
                    saw_rate_limit = True
                if attempt < max_tries - 1:
                    wait = 2 ** attempt
                    logger.debug("yfinance %s attempt %d failed (%s) — retrying in %ds",
                                 ticker, attempt + 1, exc, wait)
                    time.sleep(wait)

    hint = (
        "Yahoo is rate-limiting you (too many requests recently). Stop the app, "
        "wait 20-30 minutes, then try ONE backtest. Avoid repeatedly refreshing the "
        "Universe tab, which triggers this."
        if saw_rate_limit else
        "Market may be closed, or the symbol/date range has no data. "
        "Try a longer date range, or connect a broker for reliable data."
    )
    raise ValueError(f"No data from yfinance for {primary} (tried {tickers}). {hint}")


def fetch_ohlcv(symbol: str, timeframe: str = "5min",
                start: Optional[str] = None, end: Optional[str] = None,
                days: int = 60) -> pd.DataFrame:
    # Provider order driven by DATA_PROVIDER: "zerodha", "angel", "fyers", or "auto".
    # "auto" tries zerodha -> angel -> fyers, then yfinance as the final fallback.
    # Data-integrity guard: excluded symbols (SEBI action / insolvent / history-
    # broken) must be blocked from ALL sources, not just Zerodha - otherwise the
    # fallback chain (Fyers/yfinance) silently serves them anyway.
    try:
        from data.excluded_symbols import is_excluded, exclusion_reason
        s_check = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
        if is_excluded(s_check):
            raise ValueError(f"{s_check} is excluded - {exclusion_reason(s_check)}")
    except ImportError:
        pass   # excluded_symbols.py not present - skip the guard, don't crash

    provider = getattr(settings, "DATA_PROVIDER", "auto").lower()
    order = {"zerodha": ["zerodha"], "angel": ["angel"], "fyers": ["fyers"]}.get(
        provider, ["zerodha", "angel", "fyers"])

    for src in order:
        if src == "zerodha" and settings.ZERODHA_ACCESS_TOKEN:
            try:
                from brokers.zerodha_client import KiteData
                return KiteData.fetch_ohlcv(symbol, timeframe, start, end, days)
            except Exception as exc:
                logger.warning("Zerodha OHLCV failed (%s) — trying next source", exc)
        if src == "angel" and all([settings.ANGEL_API_KEY, settings.ANGEL_TOTP_SECRET]):
            try:
                from brokers.angel_client import AngelClient
                return AngelClient.fetch_ohlcv(symbol, timeframe, start, end, days)
            except Exception as exc:
                logger.warning("Angel OHLCV failed (%s) — trying next source", exc)
        if src == "fyers" and settings.FYERS_ACCESS_TOKEN:
            try:
                return _fetch_fyers(symbol, timeframe, start, end, days)
            except Exception as exc:
                logger.warning("Fyers OHLCV failed (%s) — yfinance fallback", exc)
    return _fetch_yfinance(symbol, timeframe, start, end, days)


def _fetch_fyers(symbol, timeframe, start, end, days):
    from fyers_apiv3 import fyersModel
    fyers = fyersModel.FyersModel(
        client_id=settings.FYERS_APP_ID,
        token=settings.FYERS_ACCESS_TOKEN,
        is_async=False,
    )
    tf_map = {"1min":"1","3min":"3","5min":"5","15min":"15","30min":"30","1hr":"60","1day":"D"}
    resolution = tf_map.get(timeframe, "5")
    fy_symbol  = _to_fyers(symbol)

    end_dt   = datetime.now(IST) if not end   else datetime.fromisoformat(end).replace(tzinfo=IST)
    start_dt = (end_dt - timedelta(days=days)) if not start else datetime.fromisoformat(start).replace(tzinfo=IST)

    # FIX: Fyers caps INTRADAY history at 100 days per request (error code -50).
    # Daily resolution ("D") has no such limit. For intraday ranges longer than
    # FIX: Fyers caps history per request — and the cap DEPENDS on resolution:
    #   • Intraday (1,2,3,5,...,240 min): max 100 days per request.
    #   • Daily/Weekly/Monthly (D, W, M):  max 366 days per request.
    # Earlier code treated daily as one unlimited request, which broke for ranges
    # over a year (error -50). Pick the right window size, then split into chunks.
    if resolution in ("D", "W", "M"):
        step = timedelta(days=360)   # under the 366-day ceiling
    else:
        step = timedelta(days=99)    # under the 100-day ceiling

    windows = []
    cur = start_dt
    while cur < end_dt:
        w_end = min(cur + step, end_dt)
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    if not windows:                  # start_dt == end_dt edge case
        windows = [(start_dt, end_dt)]

    frames = []
    for w_start, w_end in windows:
        r = fyers.history({
            "symbol": fy_symbol, "resolution": resolution, "date_format": "1",
            "range_from": w_start.strftime("%Y-%m-%d"),
            "range_to":   w_end.strftime("%Y-%m-%d"), "cont_flag": "1",
        })
        if r.get("s") != "ok":
            # If one window fails but we already have data, keep what we have.
            if frames:
                logger.warning("Fyers history window %s–%s failed: %s",
                               w_start.date(), w_end.date(), r)
                continue
            raise ValueError(f"Fyers: {r}")
        candles = r.get("candles") or []
        if candles:
            frames.append(pd.DataFrame(
                candles, columns=["timestamp", "open", "high", "low", "close", "volume"]))

    if not frames:
        raise ValueError(f"Fyers returned no candles for {symbol} ({start_dt.date()}–{end_dt.date()})")

    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="first")]   # de-dup any overlap between windows
    return df.sort_index()


def _fetch_yfinance(symbol, timeframe, start, end, days):
    primary    = _to_yf(symbol)
    interval   = TF_TO_YF.get(timeframe, "5m")
    max_days   = YF_MAX_DAYS.get(interval, 365)
    actual_days= min(days, max_days)
    if days > max_days:
        logger.warning("Clipping %d days to %d for %s interval", days, max_days, interval)
    end_dt   = datetime.now()   if not end   else datetime.fromisoformat(end)
    start_dt = (end_dt-timedelta(days=actual_days)) if not start else datetime.fromisoformat(start)
    # FIX (bug 8): when an explicit start/end is given (the backtest always does),
    # the range itself must still respect Yahoo's per-interval history cap — e.g.
    # 5m data only goes back ~60 days. The old code only clipped `days`, so a
    # 2-year explicit range on a 5m interval was sent as-is and Yahoo rejected it.
    earliest_allowed = end_dt - timedelta(days=max_days)
    if start_dt < earliest_allowed:
        logger.warning("Clipping start %s to %s — Yahoo allows max %d days for %s",
                       start_dt.date(), earliest_allowed.date(), max_days, interval)
        start_dt = earliest_allowed
    df = _download(primary, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), interval)
    df = _flatten(df)
    df.index.name = "timestamp"
    keep = [c for c in ["open","high","low","close","volume"] if c in df.columns]
    df   = df[keep].copy()
    df.dropna(subset=["close"], inplace=True)
    if df.empty:
        raise ValueError(f"Data empty after cleaning for {symbol}. Try a different date range.")
    return df


def get_live_quote(symbol: str) -> dict:
    provider = getattr(settings, "DATA_PROVIDER", "auto").lower()
    order = {"zerodha": ["zerodha"], "angel": ["angel"], "fyers": ["fyers"]}.get(
        provider, ["zerodha", "angel", "fyers"])
    s_norm = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "")
    is_index = s_norm in ("NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "SENSEX")
    for src in order:
        if src == "zerodha" and settings.ZERODHA_ACCESS_TOKEN and not is_index:
            try:
                from brokers.zerodha_client import KiteData
                q = KiteData.get_quote(symbol)
                if q.get("ltp"):
                    return q
            except Exception as exc:
                logger.warning("Zerodha quote failed for %s (%s) — trying fallback", symbol, exc)
        if src == "angel" and all([settings.ANGEL_API_KEY, settings.ANGEL_TOTP_SECRET]):
            try:
                from brokers.angel_client import AngelClient
                return AngelClient.get_quote(symbol)
            except Exception as exc:
                logger.warning("Angel quote failed: %s", exc)
        if src == "fyers" and settings.FYERS_ACCESS_TOKEN:
            try:
                q = _fyers_quote(symbol)
                if q.get("ltp"):
                    return q
            except Exception as exc:
                # Indices (NIFTY/BANKNIFTY) sometimes return -99 on the Fyers quotes
                # endpoint. Don't fail the whole request — fall through to yfinance,
                # which reliably has index quotes (^NSEI etc.).
                level = logger.info if is_index else logger.warning
                level("Fyers quote failed for %s (%s) — trying fallback", symbol, exc)
    return _yfinance_quote(symbol)


def _fyers_quote(symbol: str) -> dict:
    from fyers_apiv3 import fyersModel
    fyers = fyersModel.FyersModel(client_id=settings.FYERS_APP_ID,
                                  token=settings.FYERS_ACCESS_TOKEN, is_async=False)
    resp  = fyers.quotes({"symbols": _to_fyers(symbol)})
    # FIX: Fyers returns {"s":"error",...} (no "d" key) on failure — e.g. market
    # closed, bad symbol, or token issue. Reading resp["d"] blindly raised
    # KeyError: 'd'. Validate the response shape before indexing into it.
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        raise ValueError(f"Fyers quote error: {resp}")
    d = resp.get("d")
    if not d or not isinstance(d, list) or not d[0].get("v"):
        raise ValueError(f"Fyers quote: no data for {symbol}: {resp}")
    q     = d[0]["v"]
    ltp   = float(q.get("lp") or 0)
    prev  = float(q.get("prev_close_price") or ltp or 0)
    return {"symbol":symbol,"ltp":ltp,"open":float(q.get("open_price") or ltp),
            "high":float(q.get("high_price") or ltp),"low":float(q.get("low_price") or ltp),
            "close":prev,
            "change_pct":round((ltp-prev)/prev*100,2) if prev else 0,
            "timestamp":datetime.now(IST).isoformat()}


def _yfinance_quote(symbol: str) -> dict:
    primary   = _to_yf(symbol)
    fallbacks = FALLBACKS.get(primary, [primary])
    for ticker in fallbacks:
        try:
            info = yf.Ticker(ticker).fast_info
            ltp  = float(info.last_price or 0)
            if ltp > 0:
                prev = float(info.previous_close or ltp)
                return {"symbol":symbol,"ltp":ltp,
                        "open":float(info.open or ltp),"high":float(info.day_high or ltp),
                        "low":float(info.day_low or ltp),"close":prev,
                        "change_pct":round((ltp-prev)/prev*100,2) if prev else 0,
                        "timestamp":datetime.now(IST).isoformat()}
        except Exception as exc:
            logger.debug("Quote %s failed: %s", ticker, exc)
    # LAST RESORT: derive a price from the most recent daily candle via the normal
    # OHLCV path (uses broker history, reliable even when live quotes and Yahoo
    # both fail — e.g. for the NIFTY index quote). Better a slightly delayed real
    # price than a stubbed 0 on the dashboard.
    try:
        df = fetch_ohlcv(symbol, timeframe="1day", days=7)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            prev_close = float(df.iloc[-2]["close"]) if len(df) > 1 else float(last["close"])
            ltp = float(last["close"])
            return {"symbol":symbol,"ltp":ltp,"open":float(last["open"]),
                    "high":float(last["high"]),"low":float(last["low"]),"close":prev_close,
                    "change_pct":round((ltp-prev_close)/prev_close*100,2) if prev_close else 0,
                    "timestamp":datetime.now(IST).isoformat(),"delayed":True}
    except Exception as exc:
        logger.debug("History-based quote fallback failed for %s: %s", symbol, exc)
    logger.warning("No live price for %s — market closed or rate-limited", symbol)
    return {"symbol":symbol,"ltp":0.0,"open":0.0,"high":0.0,"low":0.0,"close":0.0,
            "change_pct":0.0,"timestamp":datetime.now(IST).isoformat()}


def _to_fyers(symbol: str) -> str:
    s = symbol.upper().replace("NSE:","").replace("-EQ","")
    if s in ("NIFTY","NIFTY50"): return "NSE:NIFTY50-INDEX"
    if s == "BANKNIFTY":         return "NSE:NIFTYBANK-INDEX"
    return f"NSE:{s}-EQ"
