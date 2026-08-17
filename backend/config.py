"""Rush Algo — Configuration"""
from __future__ import annotations
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str  = "Rush Algo"
    DEBUG:    bool = False
    PORT:     int  = 8000
    TZ:       str  = "Asia/Kolkata"

    # ── Capital ──────────────────────────────────────────────────────────────
    TOTAL_CAPITAL:        float = 1_000_000.0   # ₹10 lakh
    MAX_TRADE_AMOUNT:     float = 30_000.0       # ₹30k per trade
    MAX_TRADE_PCT:        float = 5.0            # 5% of capital
    MAX_POSITIONS:        int   = 30
    # FIX (critical): was 20.0, which on Rs 10L capital meant the kill switch only
    # fired after a Rs 2,00,000 daily loss - with Rs 30k positions and a 1.2% stop,
    # that needs ~556 consecutive stop-outs in one day. It was effectively dead
    # code, and by the time it fired you'd already lost 20% of capital. A daily
    # circuit-breaker should stop the bleeding early, not after catastrophe.
    KILL_SWITCH_PCT:      float = 3.0            # 3% daily loss = kill (Rs 30k on 10L)
    DEFAULT_SL_PCT:       float = 4.0
    DEFAULT_TRAIL_PCT:    float = 3.0
    DEFAULT_TARGET1_PCT:  float = 4.0
    DEFAULT_TARGET2_PCT:  float = 8.0
    PARTIAL_BOOK_PCT:     float = 50.0           # book 50% at target 1

    # ── Trading hours ─────────────────────────────────────────────────────────
    ENTRY_START:         str   = "11:00"
    ENTRY_END:           str   = "14:30"
    INTRADAY_EXIT:       str   = "15:15"
    INTRADAY_LAST_EXIT:  str   = "15:25"
    MAX_HOLD_DAYS:       int   = 3      # positional carry cap (calendar days)
    SCRATCH_EOD_ENABLED: bool  = True   # scratch same-day red duds near close

    # APPROVE-FIRST: when True the scanner does NOT auto-place orders. It sends
    # a Telegram alert with Approve/Skip buttons and waits - the order is only
    # placed if you tap Approve. Safer for semi-automated trading: you stay in
    # the loop on every entry. Default False = existing auto-trade behaviour,
    # so turning this on is an explicit opt-in, not a surprise change.
    APPROVE_FIRST:       bool  = False

    # ---- US market (Finnhub) -------------------------------------------------
    FINNHUB_API_KEY:     str   = ""          # free tier: 60 calls/min, personal use
    MIN_MARKET_CAP_USD:  float = 300_000_000.0   # $300M floor for the US universe

    # ── Broker credentials ───────────────────────────────────────────────────
    FYERS_APP_ID:         str  = ""
    FYERS_SECRET_KEY:     str  = ""
    FYERS_ACCESS_TOKEN:   str  = ""
    FYERS_REDIRECT_URI:   str  = "http://localhost:8000/fyers/callback"

    ZERODHA_API_KEY:      str  = ""
    ZERODHA_API_SECRET:   str  = ""
    ZERODHA_ACCESS_TOKEN: str  = ""

    DHAN_CLIENT_ID:       str  = ""
    DHAN_ACCESS_TOKEN:    str  = ""

    # ── Angel One SmartAPI (auto-login via TOTP — no daily manual token) ──────
    ANGEL_API_KEY:        str  = ""   # from smartapi.angelbroking.com app
    ANGEL_CLIENT_ID:      str  = ""   # your Angel client code (e.g. A12345)
    ANGEL_PIN:            str  = ""   # your login PIN
    ANGEL_TOTP_SECRET:    str  = ""   # the TOTP secret (base32) from enabling TOTP
    # ── Breeze (ICICIdirect) — free historical data; daily session token ──────
    BREEZE_API_KEY:       str  = ""   # from api.icicidirect.com app
    BREEZE_API_SECRET:    str  = ""   # the secret for that app
    BREEZE_SESSION_TOKEN: str  = ""   # generated DAILY via the login URL (see breeze_client.py)
    # DATA_PROVIDER picks the PRICE data source: "angel", "fyers", "breeze", or "auto"
    # (auto = try angel, then fyers, then breeze, then yfinance).
    DATA_PROVIDER:        str  = "auto"

    # ── IndianAPI (fundamentals for the universe screener) ───────────────────
    INDIANAPI_KEY:        str  = ""   # from indianapi.in
    INDIANAPI_BASE:       str  = "https://dev.indianapi.in"   # confirmed from official docs

    # ── Backtest realism: costs & slippage (so results aren't optimistic) ──────
    # Round-trip cost is modeled per-fill. Defaults approximate Indian intraday
    # equity charges (brokerage + STT + exchange txn + GST + SEBI + stamp).
    # Expressed as a percentage of each fill's value, applied on BOTH buy & sell.
    BACKTEST_COST_PCT:      float = 0.05   # ~0.05% per side (≈0.10% round-trip)
    # Slippage: fills are worse than the ideal price by this %. Buys fill higher,
    # sells fill lower. Models the gap between your target/stop and the real fill.
    BACKTEST_SLIPPAGE_PCT:  float = 0.03   # 0.03% adverse on every fill
    # Delivery (positional/CNC) has different, usually lower, charges than intraday.
    BACKTEST_COST_PCT_DELIVERY: float = 0.03

    DEFAULT_BROKER:       str  = "paper"

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN:   str  = ""
    TELEGRAM_CHAT_ID:     str  = ""

    # ── Scanner ──────────────────────────────────────────────────────────────
    # How often the live scanner checks deployed strategies during market hours.
    # 30s halves the entry blind-spot vs 60s. Safe range ~15–60s on Fyers given
    # typical deployment counts (Fyers REST limits: 10/sec, 200/min, 100k/day).
    # Don't go below ~15s unless you have very few deployments.
    SCAN_INTERVAL_SEC:    int  = 30
    FUNDAMENTAL_REFRESH_DAYS: int = 3650
    # How long to trust a "the API genuinely has no data for this symbol"
    # tombstone before re-checking. Was referenced in two places but never
    # DEFINED here: data/fundamental.py read it via getattr (so it silently
    # used a 30-day default), while refresh_fundamentals.py ASSIGNED to it -
    # and pydantic raises ValueError on assignment to an undefined field, so
    # the --force-stale flag crashed before fetching anything.
    FUNDAMENTAL_EMPTY_RECHECK_DAYS: int = 30
    MIN_MARKET_CAP_CR:    float = 1000.0         # ₹1000 crore minimum

    # ── Compliance ───────────────────────────────────────────────────────────
    MAX_ORDERS_PER_SEC:   int  = 9               # stay under 10 (SEBI self-managed limit)
    MAX_ORDERS_PER_DAY:   int  = 200

    # ── Screener ─────────────────────────────────────────────────────────────
    # SCREENER_LIVE=True fetches REAL fundamentals from yfinance (slow, one call
    # per stock, ~25s for the full list — but cached for FUNDAMENTAL_REFRESH_DAYS).
    # Set to False to use the instant curated placeholder list instead.
    SCREENER_LIVE:            bool  = True
    # Seconds to wait between yfinance requests. Higher = slower but less likely to
    # be rate-limited (429) by Yahoo. 1.5s is a safe default for the ~76-stock list.
    SCREENER_FETCH_DELAY_SEC: float = 1.5
    # If Yahoo returns this many 429s (even after backoff), stop hitting it and use
    # whatever was fetched/cached. Prevents pointlessly hammering a closed door.
    SCREENER_MAX_RATE_LIMIT_HITS: int = 4
    SCREENER_MIN_ROE:         float = 12.0
    SCREENER_MAX_DE:          float = 1.5
    # Minimum TTM/latest operating revenue (₹ crore) for a stock to be SHORTLISTED in
    # the universe. Sub-scale companies (tiny revenue) show flawless-looking ratios
    # that aren't durable (a ₹18 Cr company can post 100%-looking margins off a low
    # base), so they're excluded from the shortlist. They are STILL fetched, scored,
    # and stored — and fully visible in Stock Deep Dives if searched by symbol — just
    # not surfaced in the ranked universe list. Tune here without touching logic.
    MIN_REVENUE_CR:           float = 100.0
    SCREENER_MIN_PROMOTER:    float = 40.0
    # yfinance's promoter proxy (heldPercentInsiders) is unreliable for NSE, so the
    # promoter filter is OFF by default. Turn on only with a real promoter source.
    SCREENER_ENFORCE_PROMOTER: bool = False
    SCREENER_MIN_REV_GROWTH:  float = 10.0

    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    class Config:
        env_file          = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
