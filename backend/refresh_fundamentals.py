"""
Rush Algo - Refresh Fundamentals from IndianAPI (CLI runner)
==============================================================
Drives your EXISTING fetch/cache engine in data/fundamental.py. Does not
reimplement fetching - reuses _fetch_indianapi(), which already gives you
everything you asked for:

  - INCREMENTAL SAVE: each stock is written to disk the moment it's fetched
    (data_cache/fundamentals_by_symbol.json), via _save_symbol_cache() +
    _atomic_write_json(). If you Ctrl+C or the network drops mid-run, every
    stock fetched so far is already safely on disk - nothing is lost, and
    the OLD data for not-yet-refetched symbols is untouched until ITS OWN
    fetch completes and overwrites just that one record.
  - SAFE RESUME: next run picks up only what's still missing/stale - it does
    NOT re-fetch what's already fresh, so you never burn quota twice.
  - TOMBSTONING: stocks IndianAPI genuinely has no data for get marked
    _checked_empty and are skipped for `FUNDAMENTAL_EMPTY_RECHECK_DAYS` (see
    config) instead of being hit every run.

WHAT THIS SCRIPT ADDS:
  1. A clean CLI so you don't have to hit an HTTP endpoint to trigger it.
  2. `--report` : "dust the NA data" - scans the current cache and reports
     every symbol that is INCOMPLETE (missing roe/debt_to_equity/promoter/
     revenue_growth) so you can see exactly what's still dirty, without
     making any network calls.
  3. `--force-stale N` : re-check symbols tombstoned as empty even if their
     cooldown hasn't expired yet (use sparingly - costs quota).

USAGE (from C:\\rush-algo-fixed\\backend, venv active):
  python refresh_fundamentals.py --report                       # what's missing/NA (free, no network)
  python refresh_fundamentals.py --fetch                        # fetch only MISSING/stale data
  python refresh_fundamentals.py --fetch --full                 # re-fetch ALL stocks (everyone, fresh)
  python refresh_fundamentals.py --fetch --full --rebuild-universe   # full refresh + rebuild ranked list
"""
from __future__ import annotations
import argparse
import sys


# Symbols that structurally CANNOT have roe/debt_to_equity/promoter_holding -
# ETFs, index funds, InvITs, REITs, bond funds. No amount of re-fetching will
# ever fill these in; IndianAPI has no such data for them because it doesn't
# exist. Detected by common naming patterns (ETF/BEES/NIFTY/GOLD-fund suffixes
# etc.) - this is a heuristic, not perfect, but keeps you from burning quota
# on the same ~150-300 non-equity instruments every single run.
_NON_EQUITY_PATTERNS = (
    "ETF", "BEES", "NIFTY", "SENSEX", "GOLD", "SILVER", "LIQUID", "GILT",
    "BOND", "INVIT", "CPSE", "PSUBNK", "PSUBANK", "MOM", "MID150", "SMALL250",
    "JUNIOR", "CONS", "HCETF", "TECETF", "BFSI", "HDFCSML", "ADD", "BETA",
    "ALPHA", "QUAL", "VALUE", "MOMENTUM", "AAA", "OVERNITE", "DEBT",
)

def _looks_non_equity(sym: str) -> bool:
    return any(p in sym for p in _NON_EQUITY_PATTERNS)


def cmd_report():
    """Free, no-network: show what's currently NA/incomplete in the cache."""
    from data.fundamental import _load_symbol_cache, _is_complete
    from models.schemas import FundamentalData

    by_symbol = _load_symbol_cache()
    total = len(by_symbol)
    incomplete = []
    non_equity = []
    empty_tombstoned = []
    complete = 0

    for sym, rec in by_symbol.items():
        try:
            fd = FundamentalData(**{k: v for k, v in rec.items() if not k.startswith("_")})
        except Exception:
            incomplete.append((sym, "unparseable record"))
            continue
        if _is_complete(fd):
            complete += 1
        else:
            reason = []
            if fd.roe is None: reason.append("roe")
            if fd.debt_to_equity is None: reason.append("debt_to_equity")
            if fd.promoter_holding is None: reason.append("promoter_holding")
            if fd.revenue_growth is None: reason.append("revenue_growth")
            tag = ",".join(reason) if reason else "incomplete"
            if rec.get("_checked_empty"):
                empty_tombstoned.append((sym, tag, rec["_checked_empty"]))
            elif _looks_non_equity(sym):
                non_equity.append((sym, tag))
            else:
                incomplete.append((sym, tag))

    print(f"\nCached symbols: {total}")
    print(f"  COMPLETE:                         {complete}")
    print(f"  INCOMPLETE (real equity, will retry): {len(incomplete)}")
    print(f"  LIKELY NON-EQUITY (ETF/index/fund - can NEVER complete, skip): {len(non_equity)}")
    print(f"  TOMBSTONED empty (API confirmed no data - skipped until cooldown): {len(empty_tombstoned)}")

    if incomplete:
        print(f"\n--- INCOMPLETE, real equity (first 50 of {len(incomplete)}) ---")
        for sym, tag in incomplete[:50]:
            print(f"  {sym:<16} missing: {tag}")

    if non_equity:
        print(f"\n--- LIKELY NON-EQUITY, will never complete (first 30 of {len(non_equity)}) ---")
        for sym, tag in non_equity[:30]:
            print(f"  {sym:<16} missing: {tag}")
        print("  (These are ETFs/index/gold/bond funds - roe/debt/promoter don't apply to")
        print("   them. Re-fetching wastes quota. Use --fetch --skip-non-equity to exclude.)")

    if empty_tombstoned:
        print(f"\n--- TOMBSTONED (first 20 of {len(empty_tombstoned)}) ---")
        for sym, tag, ts in empty_tombstoned[:20]:
            print(f"  {sym:<16} missing: {tag:<40} checked_empty_at: {ts}")

    print(f"\nTo fetch only real remaining equity: python refresh_fundamentals.py --fetch --skip-non-equity")
    print()


def cmd_fetch(rebuild_universe: bool, force_stale_days, full: bool, skip_non_equity: bool):
    """Run the real fetch. Safe to Ctrl+C at any point - each stock is saved to
    disk (atomic write) the instant it's fetched, so old data for symbols not
    yet re-fetched is untouched until THEIR OWN fresh pull completes."""
    from config import settings

    if full:
        # FULL REFETCH: treat every cached symbol (complete + incomplete +
        # tombstoned) as needing a fresh pull. We do this WITHOUT deleting the
        # existing cache first - _fetch_indianapi() (and _save_symbol_cache
        # under it) overwrite one symbol's record at a time as each fetch
        # succeeds, so your old data for symbol #3000 is still intact and
        # valid while symbol #1 is being re-fetched. Nothing is wiped upfront.
        from data.fundamental import _load_symbol_cache
        cache = _load_symbol_cache()
        for rec in cache.values():
            rec.pop("_checked_empty", None)   # un-tombstone: allow re-check
            rec["_ts"] = "1970-01-01T00:00:00"  # force "stale" -> re-fetched
        from data.fundamental import _atomic_write_json, SYMBOL_CACHE_FILE
        _atomic_write_json(SYMBOL_CACHE_FILE, cache)
        print(f"FULL REFRESH: marked all {len(cache)} cached symbols as stale "
              f"(including tombstoned) so every one gets re-fetched this run.")
        print("Your existing data for each symbol stays on disk and valid until")
        print("THAT symbol's own fresh fetch completes and overwrites just its record.\n")
    elif force_stale_days is not None:
        # Only widen the tombstone cooldown (partial force), not a full refetch.
        settings.FUNDAMENTAL_EMPTY_RECHECK_DAYS = force_stale_days
        print(f"(forcing recheck of tombstoned-empty symbols older than {force_stale_days}d)")

    if skip_non_equity:
        # Permanently tombstone ETFs/index/gold/bond-fund symbols so they are
        # never requested again - roe/debt/promoter structurally don't exist
        # for them, so retrying is pure wasted quota.
        from data.fundamental import _load_symbol_cache, _atomic_write_json, SYMBOL_CACHE_FILE
        from datetime import datetime as _dt
        cache = _load_symbol_cache()
        n = 0
        for sym, rec in cache.items():
            if _looks_non_equity(sym) and not rec.get("_checked_empty"):
                rec["_checked_empty"] = _dt.now().isoformat()
                rec["_non_equity_skip"] = True
                n += 1
        if n:
            _atomic_write_json(SYMBOL_CACHE_FILE, cache)
            print(f"Tombstoned {n} likely non-equity symbols (ETF/index/fund) - "
                  f"will not be re-fetched.\n")

    from data.fundamental import _fetch_indianapi, _atomic_write_json, CACHE_FILE

    print("Starting IndianAPI fetch...")
    print("Safe to Ctrl+C at any time - each stock is saved to disk the moment it's fetched.\n")
    try:
        stocks = _fetch_indianapi()
    except KeyboardInterrupt:
        print("\nInterrupted - everything fetched so far is already saved. Re-run to resume.")
        sys.exit(0)

    print(f"\nFetch pass complete. {len(stocks)} stocks usable in this pass "
          f"(cache + newly fetched).")

    if rebuild_universe:
        from data.fundamental import refresh_universe
        print("Rebuilding ranked universe file from the refreshed cache...")
        approved = refresh_universe()
        print(f"Universe rebuilt: {len(approved)} approved stocks -> {CACHE_FILE}")
    else:
        print("Run with --rebuild-universe to also regenerate the ranked universe list,")
        print("or run:  python rescore_universe.py   (your existing tool) to do that separately.")


def main():
    ap = argparse.ArgumentParser(description="Refresh fundamentals from IndianAPI (incremental, safe).")
    ap.add_argument("--report", action="store_true", help="Show NA/incomplete symbols (free, no network).")
    ap.add_argument("--fetch", action="store_true", help="Fetch fresh/missing data from IndianAPI.")
    ap.add_argument("--rebuild-universe", action="store_true",
                     help="After fetching, rebuild the ranked fundamental_universe.json.")
    ap.add_argument("--force-stale", type=int, default=None, metavar="DAYS",
                     help="Also retry tombstoned-empty symbols older than DAYS (costs quota).")
    ap.add_argument("--skip-non-equity", action="store_true",
                     help="Permanently skip ETFs/index/gold/bond funds (roe/debt/promoter "
                          "don't apply to them - retrying wastes quota forever).")
    ap.add_argument("--full", action="store_true",
                     help="Re-fetch ALL cached symbols (complete + incomplete + tombstoned), "
                          "not just what's missing. Uses full API quota for every symbol. "
                          "Still incremental/safe - each symbol's old data is kept until its "
                          "own fresh fetch completes.")
    args = ap.parse_args()

    if not args.report and not args.fetch:
        ap.print_help()
        print("\nTip: run --report first (free) to see what's NA before spending API quota.")
        return

    if args.report:
        cmd_report()
    if args.fetch:
        cmd_fetch(args.rebuild_universe, args.force_stale, args.full, args.skip_non_equity)


if __name__ == "__main__":
    main()
