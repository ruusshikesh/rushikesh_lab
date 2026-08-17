"""
Rush Algo - Re-score the US universe from STORED data (no API calls)
=====================================================================
Mirrors rescore_universe.py (NSE) for the US side. Recomputes every cached
stock's score with the CURRENT engine and rebuilds the ranked universe file.

WHY THIS EXISTS: the scoring engine changed after the data was fetched -
growth-source priority, the soft growth curve, and the data-integrity penalties
were all added later. Without a rescore, the stored scores reflect the OLD
engine, so the ranking silently disagrees with the code that produced it.

ZERO NETWORK CALLS. It reads:
    data_cache_us/fundamentals_by_symbol_us.json   (base fields per stock)
    data_cache_us/extras_by_symbol_us.json         (derived multi-year metrics)
and writes:
    data_cache_us/fundamental_universe_us.json     (ranked, filtered)

SCOPE LIMIT worth knowing: only the DERIVED metrics were cached, not the raw
Finnhub responses. So this re-runs the SCORING layer over existing derived
values - it cannot re-derive those values with different logic (e.g. changing
how revenue CAGR is computed from statements would need a refetch). Every fix
made so far lives in the scoring layer, so a rescore captures all of them.

USAGE (from C:\\ALGO\\backend, venv active):
  python rescore_universe_us.py
"""
from __future__ import annotations
import json
import os
import sys

BACKEND = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BACKEND, "data_cache_us")
SYMBOL_CACHE = os.path.join(CACHE_DIR, "fundamentals_by_symbol_us.json")
EXTRAS_CACHE = os.path.join(CACHE_DIR, "extras_by_symbol_us.json")
UNIVERSE_FILE = os.path.join(CACHE_DIR, "fundamental_universe_us.json")


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _fmt_usd(v):
    if v is None:
        return "-"
    v = float(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v/div:,.2f}{suf}"
    return f"${v:,.0f}"


def main():
    if not os.path.exists(SYMBOL_CACHE):
        print(f"ERROR: {SYMBOL_CACHE} not found. Fetch the US universe first.")
        sys.exit(1)

    from config import settings
    from models.schemas import FundamentalData
    from data_us.fundamental_engine_us import compute_score_with_breakdown_us

    with open(SYMBOL_CACHE, encoding="utf-8") as f:
        by_symbol = json.load(f)
    extras_all = {}
    if os.path.exists(EXTRAS_CACHE):
        try:
            with open(EXTRAS_CACHE, encoding="utf-8") as f:
                extras_all = json.load(f)
        except Exception as exc:
            print(f"WARNING: extras cache unreadable ({exc}) - scoring on base fields only.")

    print("Re-scoring the US universe with the current 6-category engine "
          "from STORED data (no API calls)...")
    print(f"  cached stocks : {len(by_symbol)}")
    print(f"  with extras   : {len(extras_all)}")

    rescored, skipped, no_extras = 0, 0, 0
    stocks = []

    for sym, rec in by_symbol.items():
        try:
            fd = FundamentalData(**{k: v for k, v in rec.items() if not k.startswith("_")})
        except Exception:
            skipped += 1
            continue

        extras = extras_all.get(sym) or {}
        if not extras:
            no_extras += 1

        try:
            score, breakdown = compute_score_with_breakdown_us(fd, extras)
        except Exception as exc:
            print(f"  score failed for {sym}: {exc}")
            skipped += 1
            continue

        fd.score = score
        rec.update(fd.model_dump())
        # keep the breakdown alongside the extras so a low score stays explainable
        if sym in extras_all:
            extras_all[sym]["score_breakdown"] = breakdown
        rescored += 1
        stocks.append(fd)

    print(f"  re-scored     : {rescored}   (skipped {skipped}, {no_extras} had no extras)")

    _atomic_write_json(SYMBOL_CACHE, by_symbol)
    if extras_all:
        _atomic_write_json(EXTRAS_CACHE, extras_all)

    # Apply the same filters the live universe uses, so this file matches what
    # /api/us/universe would serve.
    min_cap = float(getattr(settings, "MIN_MARKET_CAP_USD", 300_000_000.0))
    approved, cut_cap, cut_roe, cut_de = [], 0, 0, 0
    for s in stocks:
        if s.market_cap_cr < min_cap:
            cut_cap += 1
            continue
        if s.roe is not None and s.roe < settings.SCREENER_MIN_ROE:
            cut_roe += 1
            continue
        if s.debt_to_equity is not None and s.debt_to_equity > settings.SCREENER_MAX_DE:
            cut_de += 1
            continue
        approved.append(s)

    approved.sort(key=lambda s: s.score, reverse=True)
    _atomic_write_json(UNIVERSE_FILE, [s.model_dump() for s in approved])

    print(f"\nUniverse rebuilt: {len(approved)} approved "
          f"(excluded {cut_cap} below {_fmt_usd(min_cap)} cap, "
          f"{cut_roe} below ROE {settings.SCREENER_MIN_ROE}%, "
          f"{cut_de} above D/E {settings.SCREENER_MAX_DE})")

    print("\nNew Top 20:")
    print(f"  {'#':<4}{'SYMBOL':<10}{'SCORE':>7}{'MKT CAP':>12}{'ROE':>9}{'D/E':>7}  NAME")
    print("  " + "-" * 78)
    for i, s in enumerate(approved[:20], 1):
        roe = f"{s.roe:.1f}%" if s.roe is not None else "-"
        de = f"{s.debt_to_equity:.2f}" if s.debt_to_equity is not None else "-"
        print(f"  {i:<4}{s.symbol:<10}{s.score:>7}{_fmt_usd(s.market_cap_cr):>12}"
              f"{roe:>9}{de:>7}  {str(s.name)[:34]}")

    print("\nDone. Restart the backend or refresh the dashboard to see the new ranking.")


if __name__ == "__main__":
    main()
