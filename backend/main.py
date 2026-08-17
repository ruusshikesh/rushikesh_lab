"""
Rush Algo — Personal Algorithmic Trading Platform
Brokers: Fyers · Zerodha · Dhan · Paper
Run:   uvicorn main:app --reload --port 8000
Docs:  http://localhost:8000/docs
"""
from __future__ import annotations
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pytz
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from models.schemas import (ApiResponse, BacktestRequest, Broker,
                              Deployment, Strategy)
from data.fetcher import fetch_ohlcv, get_live_quote
from data.fundamental import load_universe, refresh_universe
from data.storage import (load_strategies, save_strategies,
                           load_deployments, save_deployments)
from indicators.library import compute_all
from strategy.engine import evaluate_signal, check_mtf_confirmation
from backtest.engine import run_backtest
from execution.risk_manager import risk_manager
from compliance.engine import ComplianceEngine, Event
from alerts.telegram_bot import telegram
from execution.scanner import LiveScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
)
# Quiet APScheduler's routine per-run chatter (the "Running job ... / executed
# successfully" lines that print every SCAN_INTERVAL_SEC). The scanner keeps
# running normally; only the noisy INFO logging is suppressed. Real warnings/errors
# from the scheduler still surface.
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logger = logging.getLogger("rush-algo")
IST    = pytz.timezone(settings.TZ)

# ── Globals ────────────────────────────────────────────────────────────────────
compliance  = ComplianceEngine()
strategies: Dict[str, Strategy]    = load_strategies()    # FIX: persisted across restarts
deployments: Dict[str, Deployment] = load_deployments()   # FIX: persisted across restarts
ws_clients:  List[WebSocket]       = []
scanner:     Optional[LiveScanner] = None   # FIX: this was the missing piece — without it,
                                              # "Deploy" and "Forward Test" never actually ran


def _any_kill_active() -> bool:
    """
    FIX: there are TWO independent kill switches — compliance's (manual,
    rate-limit driven) and risk_manager's (auto-triggered by cumulative daily
    loss). The scanner correctly checks the OR of both before processing
    anything. Several HTTP endpoints were checking ONLY compliance.kill_active,
    which meant a user could get a "200 OK, resumed" response from /resume
    while the scanner silently refused to do anything because risk_manager's
    kill switch was the one actually active. Use this everywhere instead.
    """
    return compliance.kill_active or risk_manager.kill_active


def _kill_reason() -> str:
    if compliance.kill_active:
        return compliance.kill_reason
    if risk_manager.kill_active:
        return risk_manager.kill_reason
    return ""


async def broadcast(event: dict):
    msg  = json.dumps(event, default=str)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner
    logger.info("Rush Algo starting on :%d", settings.PORT)
    # FIX: load the fundamental universe in a REAL background thread. Previously this
    # ran synchronously here in startup (before `yield`), so when the universe scan
    # takes a long time (e.g. fetching ~3000 stocks at 1/sec = hours), the whole
    # backend stayed in "starting up" and could not answer /health — the frontend
    # then showed the backend as OFFLINE. Now startup finishes instantly and the
    # scan runs in the background; the app is responsive the entire time.
    import threading

    def _bg_load_universe():
        try:
            universe = load_universe()
            logger.info("Fundamental universe ready: %d approved stocks", len(universe))
        except Exception as exc:
            logger.warning("Fundamental load failed: %s", exc)

    threading.Thread(target=_bg_load_universe, name="universe-loader", daemon=True).start()

    # Buy & Sell Radar: compute once a day automatically (after the universe is
    # ready), so the dashboard always has a fresh snapshot without you having to
    # remember to hit refresh=true. Recomputes if today's snapshot is missing;
    # sleeps and re-checks periodically. Cheap to run - it just checks a date.
    def _bg_radar_daily():
        import time as _time
        from datetime import date as _date
        while True:
            try:
                from radar import load_snapshot, compute_radar
                snap = load_snapshot()
                if not snap or snap.get("date") != _date.today().isoformat():
                    logger.info("Radar: computing today's snapshot...")
                    compute_radar(top_n=25)
                    logger.info("Radar: snapshot ready.")
            except Exception as exc:
                logger.warning("Radar daily compute failed: %s", exc)
            _time.sleep(3600)   # re-check hourly; cheap no-op if already done today

    threading.Thread(target=_bg_radar_daily, name="radar-daily", daemon=True).start()

    # Start the live scanner — without this, deployed/forward-tested strategies
    # never actually scanned the market or placed any trades.
    scanner = LiveScanner(
        deployments, compliance, ws_broadcast=broadcast,
        save_deployments_fn=lambda: save_deployments(deployments),
    )
    scanner.start()

    # ── Semi-auto trading: wire the Telegram approve button to the REAL broker ───
    # When you tap ✅ Approve on a signal alert, this places the order.
    #
    # FIXED (3 critical bugs that would have cost real money):
    #  1) It used to hardcode `FyersClient.place_order()` regardless of which
    #     broker the deployment used AND regardless of paper_mode - so tapping
    #     Approve while in PAPER mode fired a REAL Fyers order. Now it routes
    #     through the scanner's own broker factory (_broker_for), which honours
    #     the deployment's broker AND the paper flag.
    #  2) The placed order was never added to dep.open_orders, so the scanner
    #     never managed it - no stop-loss, no target booking, no trailing, no
    #     EOD exit. The position sat completely unmanaged. Now it's registered
    #     so the normal exit logic picks it up on the next scan.
    #  3) The Order was built with no trade_type, so brokers defaulted it to
    #     INTRADAY/MIS - meaning an approved POSITIONAL trade would be silently
    #     auto-squared-off at 3:20pm. Now trade_type comes from the signal.
    def _place_from_signal(sig: dict) -> str:
        from models.schemas import Order
        from data.storage import save_deployments as _save_deps

        dep_id = sig.get("dep_id")
        dep = next((d for d in deployments if d.id == dep_id), None) if dep_id else None
        if dep is None:
            # No deployment context on the signal - refuse rather than guess a
            # broker. Guessing is what caused the original paper/live bug.
            raise RuntimeError(
                "Cannot place order: signal has no dep_id, so the correct broker "
                "and paper/live mode can't be determined."
            )

        if scanner is None:
            raise RuntimeError("Scanner not running - cannot place approved order.")

        order = Order(
            symbol=sig["symbol"],
            side=sig.get("side", "BUY"),
            qty=int(sig["qty"]),
            order_type=sig.get("order_type", "LIMIT"),
            price=float(sig["price"]),
            stop_loss=float(sig.get("stop_loss", 0)),
            target1=float(sig.get("target1", 0)),
            target2=float(sig.get("target2", 0)),
            broker=dep.broker,
            strategy_id=sig.get("strategy_id", ""),
            strategy_name=sig.get("strategy_name", ""),
            algo_id=sig.get("algo_id", ""),
            trade_type=sig.get("trade_type"),      # FIX 3: preserve positional vs intraday
        )

        broker = scanner._broker_for(dep)           # FIX 1: right broker + paper mode
        placed = broker.place_order(order)

        # FIX 2: register with the deployment so the scanner manages exits.
        try:
            from models.schemas import OrderStatus
            if getattr(placed, "status", None) == OrderStatus.OPEN:
                dep.open_orders.append(placed)
                dep.trade_count += 1
                compliance.record_order()
                _save_deps(deployments)
        except Exception as exc:
            logger.error("Approved order placed but FAILED to register for exit "
                         "management (%s) - check position manually: %s", exc, placed)

        return getattr(placed, "order_id", None) or str(placed)

    telegram.register_order_callback(_place_from_signal)

    # Long-poll Telegram in the background so button taps are received without
    # needing a public HTTPS webhook (ideal for a home machine).
    threading.Thread(target=telegram.poll_updates,
                     name="telegram-poller", daemon=True).start()
    yield
    if scanner is not None:
        scanner.stop()
    logger.info("Rush Algo stopped")


app = FastAPI(
    title="Rush Algo — Personal Trading Platform",
    version="1.0.0",
    description="Personal algo trading — Fyers · Zerodha · Dhan · Paper",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["meta"])
def health():
    return {
        "status":      "ok",
        "app":         settings.APP_NAME,
        "capital":     settings.TOTAL_CAPITAL,
        "strategies":  len(strategies),
        "deployments": len(deployments),
        "kill_switch": _any_kill_active(),
        "time_ist":    datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/quote/{symbol}", tags=["data"])
def quote(symbol: str):
    try:
        return ApiResponse(success=True, message="ok",
                           data=get_live_quote(symbol.upper()))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/live/subscribe", tags=["data"])
def live_subscribe(body: dict):
    """Set which symbols to stream live (the rows currently on screen).
    Body: {"symbols": ["RELIANCE", "TCS", ...]}"""
    try:
        from brokers.kite_ticker import ticker_service
        symbols = body.get("symbols") or []
        # client id keeps each panel's subscription separate - the service
        # streams the UNION, so one panel can't unsubscribe another's symbols.
        client = body.get("client") or "default"
        n = ticker_service.set_symbols(symbols, client=client)
        return ApiResponse(success=True, message=f"streaming {n} symbols",
                           data={"subscribed": n})
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/live/ticks", tags=["data"])
def live_ticks(symbols: str = ""):
    """Latest live prices for the given symbols (comma-separated).
    Returns live tick if streaming, else last close so the UI always has a price."""
    try:
        from brokers.kite_ticker import ticker_service
        syms = [s for s in symbols.split(",") if s.strip()]
        data = ticker_service.get_ticks(syms) if syms else {}
        return ApiResponse(success=True, message="ok", data=data)
    except Exception as e:
        raise HTTPException(400, str(e))


# ==============================================================================
# BUY & SELL RADAR
# Screens the fundamental universe for deep-pullback (buy) / strong-bounce
# (sell) setups. A screener/ranking, not an auto-trade signal.
# ==============================================================================

@app.get("/api/radar", tags=["data"])
def radar_get(refresh: bool = False, top_n: int = 25, date: str = None):
    """Get today's Buy & Sell radar. Uses today's saved snapshot if present,
    unless refresh=true (recomputes now) or date='YYYY-MM-DD' for history."""
    try:
        from radar import compute_radar, load_snapshot
        if date:
            snap = load_snapshot(date)
            if not snap:
                raise HTTPException(404, f"No radar snapshot for {date}")
            return ApiResponse(success=True, message="ok", data=snap)

        if not refresh:
            snap = load_snapshot()
            from datetime import date as _date
            if snap and snap.get("date") == _date.today().isoformat():
                return ApiResponse(success=True, message="ok (cached today)", data=snap)

        buy_list, sell_list = compute_radar(top_n=top_n)
        from radar import load_snapshot as _reload
        return ApiResponse(success=True, message="ok (recomputed)", data=_reload())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/radar/history", tags=["data"])
def radar_history():
    """List dates that have a saved radar snapshot."""
    try:
        from radar import list_available_dates
        return ApiResponse(success=True, message="ok", data=list_available_dates())
    except Exception as e:
        raise HTTPException(400, str(e))


# ==============================================================================
# US MARKET - fully separate module (data_us/, radar_us.py). No shared code
# path with the NSE endpoints above - a bug here cannot affect NSE data.
# ==============================================================================

@app.get("/api/us/universe", tags=["data-us"])
def get_universe_us():
    try:
        from data_us.fundamental_us import load_universe_us
        stocks = load_universe_us()
        return ApiResponse(success=True, message=f"{len(stocks)} US stocks in universe",
                           data=[s.model_dump() for s in stocks])
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/us/universe/refresh", tags=["data-us"])
def refresh_universe_us_route():
    """Kick off the US fundamentals refresh in the BACKGROUND and return
    immediately. A full refresh takes many minutes (thousands of symbols,
    throttled), which would blow past any HTTP timeout if run inline. The
    frontend polls /api/us/universe/progress for live status instead.

    Safe to call more than once: the fetch skips symbols already cached fresh,
    and each symbol is written to disk atomically the moment it's fetched, so
    an extra click costs some API quota at worst - it never loses data."""
    try:
        from data_us.fundamental_us import refresh_universe_us, get_progress
        prog = get_progress()
        if prog.get("running"):
            return ApiResponse(success=True, message="already running",
                               data=prog)

        import threading

        def _run():
            try:
                refresh_universe_us()
            except Exception as exc:
                logger.error("US universe refresh failed: %s", exc)

        threading.Thread(target=_run, name="us-universe-refresh", daemon=True).start()
        return ApiResponse(success=True, message="refresh started",
                           data=get_progress())
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/us/universe/stop", tags=["data-us"])
def us_universe_stop():
    """Ask a running US fetch to stop after the current symbol finishes.

    Cooperative, not a kill: the in-flight symbol completes and is saved, so no
    work is lost. Resuming is simply clicking Refresh again - already-cached
    symbols are skipped, so it continues from where it stopped."""
    try:
        from data_us.fundamental_us import request_stop
        return ApiResponse(success=True, message="stop requested", data=request_stop())
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/us/universe/progress", tags=["data-us"])
def us_universe_progress():
    """Live progress of the US fundamentals fetch (polled by the frontend)."""
    try:
        from data_us.fundamental_us import get_progress
        return ApiResponse(success=True, message="ok", data=get_progress())
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/us/stock/deep-dive/{symbol}", tags=["data-us"])
def deep_dive_us_route(symbol: str):
    """Full US deep dive: fundamentals + extras (industry, multi-year derived
    metrics, FCF, analyst consensus, insider activity, score breakdown).
    Extras live in a US-only store so the NSE-shared schema stays untouched."""
    try:
        from data_us.fundamental_us import deep_dive_us
        data = deep_dive_us(symbol.upper())
        if not data:
            raise HTTPException(404, f"No US data for {symbol}")
        return ApiResponse(success=True, message="ok", data=data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/us/extras/{symbol}", tags=["data-us"])
def us_extras_route(symbol: str):
    """Cached US extras for one symbol (no network call) - derived metrics,
    sentiment, score breakdown. Used where a fresh deep-dive fetch is overkill."""
    try:
        from data_us.fundamental_us import get_extras
        return ApiResponse(success=True, message="ok", data=get_extras(symbol))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/us/radar", tags=["data-us"])
def radar_us_route(refresh: bool = False, top_n: int = 25, date: str = None):
    try:
        from radar_us import compute_radar_us, load_snapshot_us
        if date:
            snap = load_snapshot_us(date)
            if not snap:
                raise HTTPException(404, f"No US radar snapshot for {date}")
            return ApiResponse(success=True, message="ok", data=snap)

        if not refresh:
            snap = load_snapshot_us()
            from datetime import date as _date
            if snap and snap.get("date") == _date.today().isoformat():
                return ApiResponse(success=True, message="ok (cached today)", data=snap)

        compute_radar_us(top_n=top_n)
        snap = load_snapshot_us()
        return ApiResponse(success=True, message="ok (recomputed)", data=snap)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/ohlcv/{symbol}", tags=["data"])
def ohlcv(symbol: str, timeframe: str = "5min", days: int = 60):
    try:
        df = fetch_ohlcv(symbol.upper(), timeframe=timeframe, days=days)
        df.index = df.index.astype(str)
        return ApiResponse(success=True, message="ok",
                           data=df.reset_index().to_dict(orient="records"))
    except Exception as e:
        raise HTTPException(400, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# FUNDAMENTAL UNIVERSE
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/universe", tags=["fundamental"])
def get_universe():
    """Approved stock universe — market cap >₹1000Cr + fundamental filters."""
    try:
        # FIX: read whatever is already built/cached WITHOUT triggering a blocking
        # rebuild. The old code called load_universe(), which — if the cache file was
        # missing or mid-build — kicked off a full multi-hour fetch on this request
        # thread, hanging the dashboard. Now we return the current approved list
        # (which grows as the background scan progresses) and never block here.
        from data.fundamental import approved_from_cache
        stocks = approved_from_cache()
        return ApiResponse(success=True,
                           message=f"{len(stocks)} stocks in universe",
                           data=[s.model_dump() for s in stocks[:5000]])
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/stock/deep-dive/{symbol}", tags=["fundamental"])
def stock_deep_dive(symbol: str):
    """
    Full fundamental profile for ONE stock for the Stock Deep Dives dashboard:
    extracted fields, six-category score breakdown, and chart-ready series
    (revenue/profit trend, margins, debt, cash flow, shareholding, peers, analysts)
    — all from the stored raw IndianAPI response. No network call.
    """
    try:
        from data.fundamental import deep_dive
        result = deep_dive(symbol)
        if result is None:
            raise HTTPException(404, f"No cached data for '{symbol}'. "
                                     f"It may not be in the universe yet.")
        return ApiResponse(success=True,
                           message=f"Deep dive for {result['symbol']}",
                           data=result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))



@app.post("/api/universe/refresh", tags=["fundamental"])
def universe_refresh():
    """Kick off a fundamental-universe refresh in the background (non-blocking).

    FIX (real bug): this function was fully written but had NO route decorator,
    so /api/universe/refresh was never registered - the dashboard's NSE Refresh
    button was silently hitting a 404 and doing nothing at all. Adding the
    decorator makes the button actually work."""
    try:
        # Run the refresh in a background thread and return immediately, instead
        # of blocking the HTTP request for the entire multi-hour scan (which made the
        # dashboard hang / appear offline).
        import threading
        from data.fundamental import get_scan_progress
        prog = get_scan_progress()
        if prog.get("running"):
            return ApiResponse(success=True, message="already running", data=prog)

        t = threading.Thread(
            target=lambda: refresh_universe(), name="universe-refresh", daemon=True)
        t.start()
        return ApiResponse(success=True,
                           message="Universe refresh started in background",
                           data=get_scan_progress())
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/universe/stop", tags=["fundamental"])
def universe_stop():
    """Ask a running NSE universe scan to stop after the current symbol.
    Cooperative - the in-flight symbol is saved first, so nothing is lost.
    Click Refresh to resume from where it stopped."""
    try:
        from data.fundamental import request_scan_stop
        return ApiResponse(success=True, message="stop requested", data=request_scan_stop())
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/universe/progress", tags=["fundamental"])
def universe_progress():
    """Live progress of the NSE fundamentals scan (polled by the frontend)."""
    try:
        from data.fundamental import get_scan_progress
        return ApiResponse(success=True, message="ok", data=get_scan_progress())
    except Exception as e:
        raise HTTPException(400, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/strategies", tags=["strategy"])
def list_strategies():
    return ApiResponse(success=True, message="ok",
                       data=[s.model_dump() for s in strategies.values()])


@app.post("/api/strategies", tags=["strategy"])
def create_strategy(strategy: Strategy):
    strategy.id = strategy.id or str(uuid.uuid4())[:8]
    strategies[strategy.id] = strategy
    save_strategies(strategies)   # FIX: persist immediately — survives restart
    compliance.log(Event.COMPLIANCE_WARN, symbol="",
                   event="strategy_created", name=strategy.name)
    return ApiResponse(success=True, message=f"Strategy '{strategy.name}' saved",
                       data=strategy.model_dump())


@app.put("/api/strategies/{sid}", tags=["strategy"])
def update_strategy(sid: str, strategy: Strategy):
    if sid not in strategies:
        raise HTTPException(404, "Strategy not found")
    strategy.id = sid
    strategies[sid] = strategy
    save_strategies(strategies)   # FIX: persist immediately
    return ApiResponse(success=True, message="Updated",
                       data=strategy.model_dump())


@app.delete("/api/strategies/{sid}", tags=["strategy"])
def delete_strategy(sid: str):
    if sid not in strategies:
        raise HTTPException(404, "Strategy not found")
    # Don't delete if deployed
    active = [d for d in deployments.values() if d.strategy.id == sid]
    if active:
        raise HTTPException(400, f"Strategy is deployed ({len(active)} active). Stop deployments first.")
    strategies.pop(sid)
    save_strategies(strategies)   # FIX: persist deletion immediately
    return ApiResponse(success=True, message="Deleted")


@app.post("/api/strategies/{sid}/signal", tags=["strategy"])
def get_signal(sid: str, symbol: Optional[str] = None):
    """Evaluate signal for a strategy on a given symbol, with MTF confirmation."""
    if sid not in strategies:
        raise HTTPException(404, "Strategy not found")
    strat = strategies[sid]
    # FIX: was `(symbol or strat.primary_tf.value and strat.name).upper()` which
    # evaluated to the STRATEGY NAME, not a symbol, due to Python's `and`/`or` chaining.
    sym = (symbol or strat.symbol or "RELIANCE").upper()
    strat_for_eval = strat.model_copy(update={"symbol": sym})
    try:
        df     = fetch_ohlcv(sym, timeframe=str(strat.primary_tf.value), days=60)
        df_ind = compute_all(df)
        result = evaluate_signal(strat_for_eval, df_ind)

        # FIX: check_mtf_confirmation was defined but never called anywhere.
        # Wire it in here so MTF actually gates the signal as designed.
        if result["signal"] == "BUY" and strat.mtf.enabled:
            mtf_fetch = lambda s, tf: fetch_ohlcv(s, timeframe=tf, days=60)
            confirmed, mtf_reason = check_mtf_confirmation(strat_for_eval, mtf_fetch)
            result["mtf_confirmed"] = confirmed
            result["mtf_reason"]    = mtf_reason
            if not confirmed:
                result["signal"] = "WATCH"
                result["reasons"].append(f"MTF rejected: {mtf_reason}")

        return ApiResponse(success=True, message="ok", data=result)
    except Exception as e:
        raise HTTPException(400, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/backtest", tags=["backtest"])
def backtest(req: BacktestRequest):
    try:
        df     = fetch_ohlcv(req.symbol,
                             timeframe=str(req.strategy.primary_tf.value),
                             start=req.start_date, end=req.end_date)
        result = run_backtest(req, df)
        return ApiResponse(success=True, message="Backtest complete",
                           data=result.model_dump())
    except Exception as e:
        logger.exception("Backtest error")
        raise HTTPException(400, str(e))


@app.post("/api/backtest/cross-sectional", tags=["backtest"])
def backtest_cross_sectional(
    start_date: str,
    end_date: str,
    top_n: int = 20,
    lookback_bars: int = 252,
    skip_bars: int = 21,
    rebalance_bars: int = 21,
    min_momentum: float = 0.0,
    initial_capital: float = 1_000_000.0,
):
    """
    Institutional cross-sectional momentum: rank the WHOLE fundamental universe by
    12-1 month momentum, hold the top-N, rebalance monthly. This is portfolio-level
    factor investing, distinct from the per-stock signal backtest. The universe is
    the same quality-screened list the Universe panel built.
    """
    from data.fundamental import approved_from_cache
    from backtest.cross_sectional import run_cross_sectional, CrossSectionalRequest

    universe = approved_from_cache()
    if not universe:
        raise HTTPException(400, "Universe is empty — load the Universe screener first")
    symbols = [s.symbol for s in universe]

    try:
        req = CrossSectionalRequest(
            symbols=symbols, start_date=start_date, end_date=end_date,
            initial_capital=initial_capital, top_n=top_n,
            lookback_bars=lookback_bars, skip_bars=skip_bars,
            rebalance_bars=rebalance_bars, min_momentum=min_momentum)
        result = run_cross_sectional(req)
        return ApiResponse(success=True, message="Cross-sectional backtest complete",
                           data=result.__dict__)
    except Exception as e:
        logger.exception("Cross-sectional backtest error")
        raise HTTPException(400, str(e))


@app.post("/api/backtest/batch", tags=["backtest"])
def backtest_batch(req: BacktestRequest):
    """
    Run the strategy across EVERY quality-approved stock in the universe
    (the same list the Universe screener already filtered by market cap /
    ROE / debt). Returns a ranked summary per stock — NOT full equity curves,
    to keep the payload light when testing dozens of stocks.

    The 'symbol' on the incoming request is ignored; the universe drives it.
    """
    # Use the already-built universe (non-blocking); don't trigger a multi-hour fetch
    # from inside a backtest request.
    from data.fundamental import approved_from_cache
    universe = approved_from_cache()
    if not universe:
        raise HTTPException(400, "Universe is empty — load the Universe screener first")

    timeframe = str(req.strategy.primary_tf.value)
    results: list[dict] = []
    failures: list[dict] = []

    for stock in universe:
        sym = stock.symbol
        try:
            df = fetch_ohlcv(sym, timeframe=timeframe,
                             start=req.start_date, end=req.end_date)
            # Reuse the hardened, cost-aware engine. Clone the strategy with this symbol.
            strat = req.strategy.model_copy(update={"symbol": sym})
            single_req = BacktestRequest(strategy=strat, symbol=sym,
                                         start_date=req.start_date, end_date=req.end_date,
                                         initial_capital=req.initial_capital)
            r = run_backtest(single_req, df)
            results.append({
                "symbol": sym,
                "name": stock.name,
                "fundamental_score": stock.score,
                "total_return_pct": r.total_return_pct,
                "cagr_pct": r.cagr_pct,
                "max_drawdown_pct": r.max_drawdown_pct,
                "win_rate_pct": r.win_rate_pct,
                "total_trades": r.total_trades,
                "sharpe_ratio": r.sharpe_ratio,
                "profit_factor": r.profit_factor,
                "score": r.score,
                "score_grade": r.score_grade,
            })
        except Exception as exc:
            failures.append({"symbol": sym, "reason": str(exc)[:120]})
            logger.warning("Batch backtest skipped %s: %s", sym, exc)

    # Rank by backtest score (best strategy fit first).
    results.sort(key=lambda x: x["score"], reverse=True)
    return ApiResponse(
        success=True,
        message=f"Batch complete: {len(results)} tested, {len(failures)} skipped",
        data={"results": results, "failures": failures,
              "tested": len(results), "skipped": len(failures)},
    )


@app.post("/api/forward-test/{sid}", tags=["backtest"])
def forward_test(sid: str):
    """Enable paper trading (forward test) for a strategy."""
    if sid not in strategies:
        raise HTTPException(404, "Strategy not found")
    strategies[sid].paper_mode = True
    save_strategies(strategies)   # FIX: mutation was never persisted — lost on restart
    return ApiResponse(success=True, message="Forward test (paper mode) enabled")


# ══════════════════════════════════════════════════════════════════════════════
# DEPLOYMENTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/deployments", tags=["deploy"])
def list_deployments():
    return ApiResponse(success=True, message="ok",
                       data=[d.model_dump() for d in deployments.values()])


@app.post("/api/deployments", tags=["deploy"])
def deploy(strategy: Strategy):
    if len(deployments) >= settings.MAX_POSITIONS:
        raise HTTPException(400, f"Max {settings.MAX_POSITIONS} deployments reached")
    if _any_kill_active():
        raise HTTPException(400, f"Kill switch active: {_kill_reason()}")
    strategy.id = strategy.id or str(uuid.uuid4())[:8]
    strategies[strategy.id] = strategy
    save_strategies(strategies)   # FIX: persist strategy used for this deployment
    dep = Deployment(id=str(uuid.uuid4())[:8], strategy=strategy,
                     broker=strategy.broker, paper_mode=strategy.paper_mode)
    deployments[dep.id] = dep
    save_deployments(deployments)   # FIX: persist deployment
    compliance.log(Event.COMPLIANCE_WARN, dep_id=dep.id,
                   event="deployed", strategy=strategy.name,
                   broker=strategy.broker.value, paper=strategy.paper_mode)
    return ApiResponse(success=True,
                       message=f"'{strategy.name}' deployed ({'PAPER' if strategy.paper_mode else 'LIVE'})",
                       data=dep.model_dump())


@app.patch("/api/deployments/{dep_id}/pause", tags=["deploy"])
def pause(dep_id: str):
    if dep_id not in deployments: raise HTTPException(404, "Not found")
    deployments[dep_id].status = "PAUSED"
    save_deployments(deployments)   # FIX: persist status change
    return ApiResponse(success=True, message="Paused")


@app.patch("/api/deployments/{dep_id}/resume", tags=["deploy"])
def resume(dep_id: str):
    if dep_id not in deployments: raise HTTPException(404, "Not found")
    if _any_kill_active():
        raise HTTPException(400, "Kill switch is active — reset it before resuming")
    deployments[dep_id].status = "LIVE"
    save_deployments(deployments)   # FIX: persist status change
    return ApiResponse(success=True, message="Resumed")


@app.delete("/api/deployments/{dep_id}", tags=["deploy"])
def stop_deploy(dep_id: str):
    if dep_id not in deployments: raise HTTPException(404, "Not found")
    deployments.pop(dep_id)
    save_deployments(deployments)   # FIX: persist removal
    return ApiResponse(success=True, message="Stopped")


# ══════════════════════════════════════════════════════════════════════════════
# P&L
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pnl", tags=["portfolio"])
def pnl():
    deps = list(deployments.values())
    risk_status = risk_manager.status()
    # FIX: same class of bug as deploy/resume — risk_status alone only reflects
    # risk_manager's kill switch, not compliance's. Use the combined truth.
    risk_status["kill_active"] = _any_kill_active()
    risk_status["kill_reason"] = _kill_reason()
    return ApiResponse(success=True, message="ok", data={
        **risk_status,
        "today_pnl":   round(sum(d.today_pnl for d in deps), 2),
        "total_pnl":   round(sum(d.total_pnl for d in deps), 2),
        "trade_count": sum(d.trade_count for d in deps),
        "deployments": len(deps),
    })


@app.get("/api/positions", tags=["portfolio"])
def positions():
    orders = []
    for dep in deployments.values():
        for o in dep.open_orders:
            orders.append(o.model_dump())
    return ApiResponse(success=True, message="ok", data=orders)


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/compliance/kill", tags=["compliance"])
def kill_switch(reason: str = "Manual"):
    compliance.activate_kill(reason)
    risk_manager.activate_kill(reason)
    for dep in deployments.values():
        dep.status = "PAUSED"
    save_deployments(deployments)   # FIX: was never persisted, unlike pause/resume/stop
    telegram.kill_switch_alert(reason, risk_manager.daily_pnl)
    return ApiResponse(success=True, message=f"Kill switch ACTIVE",
                       data=compliance.status())


@app.post("/api/compliance/reset", tags=["compliance"])
def reset_kill():
    compliance.reset_kill()
    risk_manager.reset_kill()
    return ApiResponse(success=True, message="Kill switch reset",
                       data=compliance.status())


@app.get("/api/compliance/status", tags=["compliance"])
def compliance_status():
    # FIX: was {**compliance.status(), **risk_manager.status()} — both dicts
    # have their own kill_active/kill_reason keys, so the merge order silently
    # decided which one "won". Compute the combined truth explicitly instead.
    merged = {**compliance.status(), **risk_manager.status()}
    merged["kill_active"] = _any_kill_active()
    merged["kill_reason"] = _kill_reason()
    return ApiResponse(success=True, message="ok", data=merged)


@app.get("/api/compliance/audit", tags=["compliance"])
def compliance_audit(limit: int = 100):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit must be 1–1000")
    return ApiResponse(success=True, message="ok",
                       data=compliance.get_audit(limit))


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/eod", tags=["reports"])
def eod_report():
    deps = list(deployments.values())
    # FIX: closed orders are removed from open_orders when closed — they belong
    # in closed_trades. Previously this always scanned open_orders for CLOSED
    # status, which never matched anything, so the report always showed 0 trades.
    all_orders = []
    for dep in deps:
        for o in dep.closed_trades:
            all_orders.append(o.model_dump())

    total_pnl = sum(d.today_pnl for d in deps)
    winners   = [o for o in all_orders if (o.get("pnl") or 0) > 0]
    losers    = [o for o in all_orders if (o.get("pnl") or 0) < 0]

    report = {
        "date":        datetime.now(IST).strftime("%Y-%m-%d"),
        "total_pnl":   round(total_pnl, 2),
        "trade_count": len(all_orders),
        "winners":     len(winners),
        "losers":      len(losers),
        "win_rate":    round(len(winners)/len(all_orders)*100 if all_orders else 0, 1),
        "capital":     settings.TOTAL_CAPITAL,
        "return_pct":  round(total_pnl/settings.TOTAL_CAPITAL*100, 2),
        "trades":      all_orders,
    }

    telegram.eod_report(all_orders, total_pnl, len(winners), len(losers))
    return ApiResponse(success=True, message="EOD Report", data=report)


# ══════════════════════════════════════════════════════════════════════════════
# BROKER AUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/broker/auth-url/{broker}", tags=["auth"])
def broker_auth_url(broker: str):
    b = broker.lower()
    if b == "fyers":
        from brokers.fyers_client import FyersClient
        return {"broker": "fyers", "url": FyersClient.get_auth_url()}
    if b == "zerodha":
        from brokers.zerodha_client import ZerodhaClient
        return {"broker": "zerodha", "url": ZerodhaClient.get_login_url()}
    if b == "dhan":
        return {"broker": "dhan", "url": "https://web.dhan.co/",
                "note": "Generate token from Dhan portal"}
    raise HTTPException(400, f"Unknown broker: {broker}")


@app.get("/fyers/callback", tags=["auth"])
def fyers_callback(code: str = ""):
    if not code: raise HTTPException(400, "No auth code")
    from brokers.fyers_client import FyersClient
    token = FyersClient.exchange_code(code)
    return {"FYERS_ACCESS_TOKEN": token}


@app.get("/zerodha/callback", tags=["auth"])
def zerodha_callback(request_token: str = ""):
    if not request_token: raise HTTPException(400, "No request_token")
    from brokers.zerodha_client import ZerodhaClient
    token = ZerodhaClient.exchange_token(request_token)
    return {"ZERODHA_ACCESS_TOKEN": token}


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)

    # Per-connection live-price state: the browser sends {type:"watch", symbols:[...]}
    # (its on-screen rows); we push live prices for those symbols as they change —
    # no HTTP polling. Prices come from the KiteTicker stream (ticker_service).
    watch: List[str] = []
    last_sent: Dict[str, float] = {}

    async def price_pusher():
        from brokers.kite_ticker import ticker_service
        while True:
            try:
                if watch:
                    data = await asyncio.to_thread(ticker_service.get_ticks, watch)
                    changed = {}
                    for sym, q in data.items():
                        ltp = q.get("ltp")
                        if ltp is not None and last_sent.get(sym) != ltp:
                            last_sent[sym] = ltp
                            changed[sym] = q
                    if changed:
                        await ws.send_text(json.dumps({"type": "prices", "data": changed}, default=str))
                await asyncio.sleep(0.25)   # push up to 4x/sec, only when values change
            except Exception:
                await asyncio.sleep(0.5)

    pusher = asyncio.create_task(price_pusher())
    try:
        await ws.send_text(json.dumps({
            "type": "connected", "message": "Rush Algo live feed",
            "timestamp": datetime.now(IST).isoformat(),
        }))
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30)
                try:
                    m = json.loads(raw)
                    if m.get("type") == "watch":
                        syms = m.get("symbols") or []
                        watch.clear(); watch.extend(syms)
                        # subscribe on the ticker stream (background, non-blocking)
                        try:
                            from brokers.kite_ticker import ticker_service
                            cid = m.get("client") or f"ws-{id(ws)}"
                            await asyncio.to_thread(ticker_service.set_symbols, syms, cid)
                        except Exception:
                            pass
                except Exception:
                    pass
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        if ws in ws_clients:
            ws_clients.remove(ws)
    finally:
        pusher.cancel()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT,
                reload=settings.DEBUG)
