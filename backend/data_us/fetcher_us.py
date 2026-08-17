"""
Rush Algo - US Price Fetcher (yfinance)
=========================================
US OHLCV via yfinance. yfinance is US-native (built for US tickers originally,
unlike its awkward retrofit for NSE), so no ".NS" suffix juggling, no 60-day
intraday caps mattering much for our use case (Radar/Deep Dive use DAILY bars,
which yfinance serves for years, not just 60 days).

Fully separate from data/fetcher.py (NSE) - no shared code path.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_ohlcv_us(symbol: str, timeframe: str = "1day",
                   start: Optional[str] = None, end: Optional[str] = None,
                   days: int = 400) -> pd.DataFrame:
    """US daily OHLCV. Ticker is used as-is (AAPL, MSFT, etc - no suffix
    needed). Retries once on empty/failed response (Yahoo occasionally
    throttles), matching the NSE fetcher's resilience pattern."""
    interval_map = {"1day": "1d", "1hr": "60m", "5min": "5m", "15min": "15m"}
    interval = interval_map.get(timeframe, "1d")

    end_dt = datetime.now() if not end else datetime.fromisoformat(end)
    start_dt = (end_dt - timedelta(days=days)) if not start else datetime.fromisoformat(start)

    for attempt in range(2):
        try:
            df = yf.download(symbol, start=start_dt.strftime("%Y-%m-%d"),
                            end=end_dt.strftime("%Y-%m-%d"), interval=interval,
                            progress=False, auto_adjust=True)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [str(c[0]).lower() for c in df.columns]
                else:
                    df.columns = [str(c).lower() for c in df.columns]
                df.index.name = "timestamp"
                keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
                df = df[keep].dropna(subset=["close"])
                return df.sort_index()
        except Exception as exc:
            logger.debug("US OHLCV %s attempt %d failed: %s", symbol, attempt + 1, exc)
    raise ValueError(f"No US OHLCV data for {symbol} ({start_dt.date()}-{end_dt.date()})")


def get_live_quote_us(symbol: str) -> dict:
    """Last-close style quote (US module has no live streaming yet - matches
    the stated 'no execution/trading yet' scope). Uses yfinance fast_info."""
    try:
        info = yf.Ticker(symbol).fast_info
        ltp = float(info.last_price or 0)
        prev = float(info.previous_close or ltp or 0)
        if ltp > 0:
            return {"symbol": symbol, "ltp": ltp,
                    "open": float(info.open or ltp), "high": float(info.day_high or ltp),
                    "low": float(info.day_low or ltp), "close": prev,
                    "change_pct": round((ltp - prev) / prev * 100, 2) if prev else 0,
                    "timestamp": datetime.now().isoformat(), "live": False}
    except Exception as exc:
        logger.debug("US quote failed for %s: %s", symbol, exc)
    return {"symbol": symbol, "ltp": 0.0, "open": 0.0, "high": 0.0, "low": 0.0,
            "close": 0.0, "change_pct": 0.0, "timestamp": datetime.now().isoformat(), "live": False}
