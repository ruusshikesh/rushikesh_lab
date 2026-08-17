"""
Rush Algo — Multi-Stock Backtest Runner  (REGIME-FILTER version)
================================================================
Loops the momentum-pullback strategy across liquid large-caps by calling your
ALREADY-RUNNING backend's /api/backtest endpoint, then prints an aggregate report.

THIS VERSION adds the REGIME FILTER (one focused iteration):
  - ADD  EMA(50) > EMA(200)   <- only trade a sustained up-regime (the key change)
  - RAISE ADX 25 -> 28        <- only strong trends
  - DROP MFI                  <- was redundant
  - MTF require_all = True     <- both 15min AND 1hr must confirm
Everything else identical to the prior run so the comparison is clean.

REQUIREMENTS:
  - Backend RUNNING (uvicorn main:app --port 8000)
  - Fyers token set
USAGE (from C:\\rush-algo-fixed\\backend, venv active):
  python run_multi_backtest.py
"""
from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "http://localhost:8000"

# ~100 days = the most 5min history Fyers serves per request. Recent window.
START = "2025-03-24"
END   = "2025-06-30"

# Liquid large-caps only — where Fyers has clean data AND Rs 10-20 moves happen.
SYMBOLS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY",
    "TCS", "LT", "AXISBANK", "KOTAKBANK", "BHARTIARTL",
    "ITC", "HINDUNILVR", "MARUTI", "TATAMOTORS", "SUNPHARMA",
]

# The intraday momentum-pullback strategy WITH REGIME FILTER.
STRATEGY = {
    "name": "Intraday Momentum Pullback (Regime-Filtered)",
    "trade_type": "POSITIONAL",
    "primary_tf": "5min",
    "symbol": "RELIANCE",          # overridden per-symbol in the loop
    "watchlist": [],
    "entry_conditions": [
        {"indicator": "Close",   "comparator": "crosses_above", "value": "EMA(20)",  "join": "AND"},
        {"indicator": "EMA(20)", "comparator": "greater_than",  "value": "EMA(50)",  "join": "AND"},
        {"indicator": "EMA(50)", "comparator": "greater_than",  "value": "EMA(200)", "join": "AND"},
        {"indicator": "ADX",     "comparator": "greater_than",  "value": "28",       "join": "AND"},
        {"indicator": "Close",   "comparator": "greater_than",  "value": "VWAP",     "join": "AND"},
    ],
    "risk": {
        "sl_pct": 1.2, "target1_pct": 1.0, "target2_pct": 2.5,
        "trailing_sl_pct": 0.8, "partial_book_pct": 50.0,
        "trade_amount": 30000.0, "max_positions": 30,
    },
    "mtf": {
        "enabled": True, "primary_tf": "5min",
        "confirm_tfs": ["15min", "1hr"], "require_all": True,
    },
    "broker": "paper", "paper_mode": True,
}


def post_backtest(symbol: str) -> dict:
    strat = dict(STRATEGY)
    strat["symbol"] = symbol
    body = {
        "strategy": strat,
        "symbol": symbol,
        "start_date": START,
        "end_date": END,
        "initial_capital": 1_000_000.0,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/api/backtest",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(payload.get("message", "backtest failed"))
    return payload["data"]


def main():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=10) as r:
            json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"ERROR: backend not reachable at {BASE_URL} ({exc}).")
        print("Start it first:  uvicorn main:app --port 8000")
        sys.exit(1)

    print(f"\nREGIME-FILTERED | {len(SYMBOLS)} stocks | {START} -> {END} | 5min\n")
    print(f"{'SYMBOL':<12}{'TRADES':>7}{'WIN%':>7}{'NET P&L':>12}{'RET%':>8}{'PF':>7}  NOTE")
    print("-" * 72)

    all_trades = []
    total_net  = 0.0
    per_stock  = []
    failures   = []

    for sym in SYMBOLS:
        try:
            d = post_backtest(sym)
            trades = d.get("trades", [])
            net    = round(d["final_capital"] - d["initial_capital"], 2)
            total_net += net
            all_trades.extend(trades)
            per_stock.append((sym, d))
            print(f"{sym:<12}{d['total_trades']:>7}{d['win_rate_pct']:>7.1f}"
                  f"{net:>12,.0f}{d['total_return_pct']:>8.2f}{d['profit_factor']:>7.2f}")
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8")[:80] if hasattr(exc, "read") else str(exc)
            failures.append((sym, msg))
            print(f"{sym:<12}{'-':>7}{'-':>7}{'-':>12}{'-':>8}{'-':>7}  FAIL: {msg}")
        except Exception as exc:
            failures.append((sym, str(exc)[:80]))
            print(f"{sym:<12}{'-':>7}{'-':>7}{'-':>12}{'-':>8}{'-':>7}  FAIL: {str(exc)[:40]}")
        time.sleep(0.5)

    n = len(all_trades)
    wins = [t for t in all_trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in all_trades if (t.get("pnl") or 0) <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss else float("inf")
    win_rate = round(len(wins) / n * 100, 1) if n else 0.0
    avg_trade = round(sum(t["pnl"] for t in all_trades) / n, 2) if n else 0.0

    print("\n" + "=" * 72)
    print("AGGREGATE — all stocks pooled  (REGIME-FILTERED)")
    print("=" * 72)
    print(f"  Stocks tested:        {len(per_stock)} ok, {len(failures)} failed")
    print(f"  TOTAL TRADES:         {n}")
    print(f"  Win rate:             {win_rate}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Net P&L (after costs):Rs {total_net:,.2f}")
    print(f"  Avg P&L per trade:    Rs {avg_trade:,.2f}")
    print(f"  Profit factor:        {pf}")
    print(f"  Return on 10L capital:{total_net / 1_000_000 * 100:.2f}%")
    print()
    print("  vs PRIOR (no filter): 87 trades, 50.6% win, PF 0.74, -Rs 3,972")
    print()
    if pf == float("inf") or pf >= 1.0:
        print(f"  >> PF {pf} >= 1.0 — regime filter HELPED. Next: out-of-sample test on")
        print(f"     an EARLIER window to confirm it's real, not curve-fit to this period.")
    else:
        print(f"  >> PF {pf} < 1.0 — still losing. Regime filter did not rescue it.")
        print(f"     Responsible move: STOP tuning momentum. Look at swing or pairs.")
    if n < 30:
        print(f"  NOTE: only {n} trades — filter cut the sample. Judge PF cautiously;")
        print(f"        a longer history (Zerodha) would give a firmer read.")
    if failures:
        print("\n  Failed symbols:")
        for s, m in failures:
            print(f"    {s}: {m}")
    print()


if __name__ == "__main__":
    main()
