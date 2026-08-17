"""
Rush Algo — Cross-Sectional Momentum (rank & rebalance) backtest.

This is the institutional "factor investing" approach, distinct from the per-stock
signal engine in backtest/engine.py. Instead of asking "does THIS stock meet
absolute entry conditions?", it asks "of ALL stocks in the universe, which are the
strongest RIGHT NOW?" — ranks the whole universe on each rebalance date, holds the
top-N, and rotates as rankings change. This is the most robustly documented equity
anomaly (AQR-style cross-sectional momentum).

Design choices grounded in the academic literature:
  * Momentum signal = total return over a LOOKBACK window, but SKIPPING the most
    recent `skip` bars. The skip (classically the most recent 1 month on daily
    data) removes short-term mean-reversion contamination — buying a stock that
    spiked in the last week tends to revert; the 12-1 month window is the standard.
  * Equal-weight the top-N for now. Volatility-based weighting is a separate layer
    (layer 2) and plugs into the `_weights()` function below.
  * Rebalance on a fixed cadence (monthly by default). Between rebalances we hold.
  * Costs & slippage reuse the SAME settings as the per-stock engine so results are
    directly comparable and equally honest (not optimistic).

It deliberately reuses fetch_ohlcv and the config cost settings so its numbers are
comparable to the existing engine and equally cost-aware.
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import settings
from data.fetcher import fetch_ohlcv

logger = logging.getLogger(__name__)

# Absolute paths (anchored to this file, NOT the process cwd — same lesson as the
# fundamentals cache: relative paths silently resolve to different folders under
# uvicorn --reload). One cache for price history, one skiplist for symbols yahoo/
# the broker simply cannot resolve (delisted, too new, wrong ticker).
_BACKEND_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR       = os.path.join(_BACKEND_DIR, "data_cache")
_PRICE_CACHE_DIR = os.path.join(_CACHE_DIR, "price_history")
_DEAD_FILE       = os.path.join(_CACHE_DIR, "dead_symbols.json")

_ANN = {  # bars per year, for annualising Sharpe — daily only really makes sense here
    "1day": 252, "1hr": 252 * 6, "30min": 252 * 13,
    "15min": 252 * 26, "5min": 252 * 75,
}


def _load_dead() -> set:
    try:
        with open(_DEAD_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_dead(dead: set) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(dead), f)
        os.replace(tmp, _DEAD_FILE)
    except Exception as exc:
        logger.debug("dead-symbol save failed: %s", exc)


def _price_cache_path(sym: str, tf: str, start: str, end: str) -> str:
    safe = sym.replace("/", "_").replace("\\", "_")
    return os.path.join(_PRICE_CACHE_DIR, f"{safe}__{tf}__{start}__{end}.json")


def _load_cached_prices(sym: str, tf: str, start: str, end: str) -> Optional[pd.Series]:
    path = _price_cache_path(sym, tf, start, end)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
            s = pd.Series(obj["values"], index=pd.to_datetime(obj["index"]), dtype=float)
            return s
    except Exception:
        pass
    return None


def _save_cached_prices(sym: str, tf: str, start: str, end: str, close: pd.Series) -> None:
    try:
        os.makedirs(_PRICE_CACHE_DIR, exist_ok=True)
        obj = {
            "index": [str(x) for x in close.index],
            "values": [float(v) for v in close.values],
        }
        path = _price_cache_path(sym, tf, start, end)
        fd, tmp = tempfile.mkstemp(dir=_PRICE_CACHE_DIR, prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("price cache save failed for %s: %s", sym, exc)


@dataclass
class CrossSectionalRequest:
    symbols:          List[str]
    start_date:       str
    end_date:         str
    initial_capital:  float = 1_000_000.0
    timeframe:        str   = "1day"
    lookback_bars:    int   = 252          # ~12 months of daily bars
    skip_bars:        int   = 21           # ~1 month skip (the "12-1" convention)
    top_n:            int   = 20           # hold this many names
    rebalance_bars:   int   = 21           # rebalance ~monthly
    min_momentum:     float = 0.0          # only hold names with momentum above this (e.g. 0 = positive only)


@dataclass
class CrossSectionalResult:
    total_return_pct: float
    cagr_pct:         float
    max_drawdown_pct: float
    sharpe_ratio:     float
    volatility_pct:   float
    rebalances:       int
    avg_holdings:     float
    total_costs:      float
    final_capital:    float
    equity_curve:     List[dict]  = field(default_factory=list)
    rebalance_log:    List[dict]  = field(default_factory=list)
    skipped_symbols:  List[str]   = field(default_factory=list)


def _momentum_score(close: pd.Series, lookback: int, skip: int) -> Optional[float]:
    """
    Total return from (lookback+skip) bars ago up to `skip` bars ago.
    i.e. the classic "12 months ago -> 1 month ago" window. Returns None if there
    isn't enough history at this point.
    """
    if len(close) < lookback + skip + 1:
        return None
    end_px   = close.iloc[-(skip + 1)]      # price `skip` bars ago
    start_px = close.iloc[-(lookback + skip + 1)]
    if start_px <= 0 or pd.isna(start_px) or pd.isna(end_px):
        return None
    return (end_px / start_px) - 1.0


def _weights(symbols: List[str], px_history: Dict[str, pd.Series], asof_idx) -> Dict[str, float]:
    """
    Equal-weight for now (layer 1). Layer 2 (volatility-based sizing) replaces the
    body of this function with inverse-vol weights — it's isolated here on purpose
    so that change is a one-function swap.
    """
    if not symbols:
        return {}
    w = 1.0 / len(symbols)
    return {s: w for s in symbols}


def run_cross_sectional(req: CrossSectionalRequest) -> CrossSectionalResult:
    cost_pct = float(getattr(settings, "BACKTEST_COST_PCT_DELIVERY",
                     getattr(settings, "BACKTEST_COST_PCT", 0.0)))
    slip_pct = float(getattr(settings, "BACKTEST_SLIPPAGE_PCT", 0.0))

    # ── 1. Load every symbol's history once, align onto a common date index ──────
    # Three-tier loading per symbol:
    #   (a) known-dead (yfinance/broker can't resolve it) -> skip instantly, no fetch
    #   (b) price cache hit on disk                       -> instant, no network
    #   (c) otherwise fetch, then cache the result (or mark dead on failure)
    # This is what turns a 10-minute run into a few seconds on the second run, and
    # stops wasting ~10-15s of retries per delisted/non-existent symbol every run.
    dead = _load_dead()
    newly_dead: set = set()
    closes: Dict[str, pd.Series] = {}
    skipped: List[str] = []
    cache_hits = fetched = 0

    for sym in req.symbols:
        if sym in dead:
            skipped.append(sym)
            continue

        cached = _load_cached_prices(sym, req.timeframe, req.start_date, req.end_date)
        if cached is not None and not cached.empty:
            closes[sym] = cached
            cache_hits += 1
            continue

        try:
            df = fetch_ohlcv(sym, timeframe=req.timeframe,
                             start=req.start_date, end=req.end_date)
            if df is None or df.empty or "close" not in df.columns:
                skipped.append(sym); newly_dead.add(sym); continue
            close = df["close"].astype(float)
            closes[sym] = close
            _save_cached_prices(sym, req.timeframe, req.start_date, req.end_date, close)
            fetched += 1
        except Exception as exc:
            logger.warning("cross-sectional: skipped %s (%s)", sym, str(exc)[:120])
            skipped.append(sym)
            newly_dead.add(sym)   # couldn't resolve at all -> remember, skip next time

    if newly_dead:
        _save_dead(dead | newly_dead)

    logger.info("cross-sectional load: %d usable (%d from cache, %d fetched), "
                "%d skipped (%d newly marked dead)", len(closes), cache_hits,
                fetched, len(skipped), len(newly_dead))

    if len(closes) < req.top_n:
        raise ValueError(
            f"Only {len(closes)} symbols had usable data, need at least top_n="
            f"{req.top_n}. Widen the universe or date range.")

    # Common, sorted master calendar across all symbols (union of dates), forward-fill
    all_px = pd.DataFrame(closes).sort_index()
    all_px = all_px.ffill()                 # carry last price over missing days
    dates  = all_px.index

    warmup = req.lookback_bars + req.skip_bars + 1
    if len(dates) <= warmup + req.rebalance_bars:
        raise ValueError(
            f"Not enough history: {len(dates)} bars, need >{warmup + req.rebalance_bars}. "
            "Use a longer date range.")

    # ── 2. Walk forward, rebalancing every `rebalance_bars` ─────────────────────
    cash          = req.initial_capital
    holdings: Dict[str, float] = {}         # symbol -> qty
    equity_curve  = []
    rebal_log     = []
    total_costs   = 0.0
    holdings_count = []

    def portfolio_value(i) -> float:
        v = cash
        for s, qty in holdings.items():
            px = all_px[s].iloc[i]
            if not pd.isna(px):
                v += qty * px
        return v

    for i in range(warmup, len(dates)):
        is_rebal = ((i - warmup) % req.rebalance_bars == 0)

        if is_rebal:
            # rank the universe by momentum as of this bar
            scores = {}
            for s in closes:
                hist = all_px[s].iloc[: i + 1].dropna()
                m = _momentum_score(hist, req.lookback_bars, req.skip_bars)
                if m is not None and m > req.min_momentum:
                    scores[s] = m
            ranked  = sorted(scores, key=scores.get, reverse=True)
            target  = ranked[: req.top_n]
            tgt_w   = _weights(target, closes, i)

            # current portfolio value drives target rupee allocations
            pv = portfolio_value(i)

            # SELL: anything not in target, or to rebalance weights
            new_holdings = {}
            # First liquidate everything to cash at this bar's price (simple, robust
            # full-rebalance — turnover cost is modelled below, so it's honest).
            for s, qty in holdings.items():
                px = all_px[s].iloc[i]
                if pd.isna(px) or qty == 0:
                    continue
                sell_fill = px * (1 - slip_pct / 100)
                proceeds  = qty * sell_fill
                fee       = proceeds * cost_pct / 100
                cash     += proceeds - fee
                total_costs += fee
            holdings = {}

            # BUY: allocate to targets
            for s in target:
                px = all_px[s].iloc[i]
                if pd.isna(px) or px <= 0:
                    continue
                alloc     = pv * tgt_w.get(s, 0.0)
                buy_fill  = px * (1 + slip_pct / 100)
                qty       = int(alloc // buy_fill)
                if qty <= 0:
                    continue
                cost_val  = qty * buy_fill
                fee       = cost_val * cost_pct / 100
                if cost_val + fee > cash:
                    continue
                cash     -= (cost_val + fee)
                total_costs += fee
                new_holdings[s] = qty
            holdings = new_holdings

            rebal_log.append({
                "date": str(dates[i].date()) if hasattr(dates[i], "date") else str(dates[i]),
                "held": list(holdings.keys()),
                "n_held": len(holdings),
                "top_momentum": round(scores[ranked[0]] * 100, 1) if ranked else None,
            })

        pv = portfolio_value(i)
        equity_curve.append({
            "date": str(dates[i].date()) if hasattr(dates[i], "date") else str(dates[i]),
            "equity": round(pv, 2),
        })
        holdings_count.append(len(holdings))

    # ── 3. Metrics (same definitions as the per-stock engine) ───────────────────
    eq = pd.Series([e["equity"] for e in equity_curve])
    final_cap = float(eq.iloc[-1])
    total_return = (final_cap / req.initial_capital - 1) * 100

    n_bars = len(eq)
    ann    = _ANN.get(req.timeframe, 252)
    yrs    = n_bars / ann if ann else 1
    cagr   = ((final_cap / req.initial_capital) ** (1 / yrs) - 1) * 100 if yrs > 0 else total_return

    rets   = eq.pct_change().dropna()
    vol    = float(rets.std() * (ann ** 0.5) * 100) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / rets.std() * (ann ** 0.5)) if rets.std() > 0 else 0.0

    roll_max = eq.cummax()
    dd       = (eq / roll_max - 1) * 100
    max_dd   = float(dd.min()) if len(dd) else 0.0

    return CrossSectionalResult(
        total_return_pct=round(total_return, 2),
        cagr_pct=round(cagr, 2),
        max_drawdown_pct=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        volatility_pct=round(vol, 2),
        rebalances=len(rebal_log),
        avg_holdings=round(float(np.mean(holdings_count)), 1) if holdings_count else 0.0,
        total_costs=round(total_costs, 2),
        final_capital=round(final_cap, 2),
        equity_curve=equity_curve,
        rebalance_log=rebal_log,
        skipped_symbols=skipped,
    )
