"""
Re-score the ALREADY-CACHED universe with the six-category fundamental engine —
using each stock's STORED raw IndianAPI response. ZERO API calls.

This is the feedback-loop tool: whenever the engine changes (new rule, bug fix,
weight tweak), run this to recompute every stock's score + breakdown from the raw
data we already saved, so the ranking updates WITHOUT re-fetching anything.

It recomputes, for every enriched stock (those with a stored _raw):
  • the six category scores + details (_breakdown)
  • the extracted absolute fields (revenue_cr, fcf_cr, etc.) via the current parser
  • the final score
then rebuilds the assembled universe file, sorted, applying the revenue-scale
shortlist floor (MIN_REVENUE_CR) so sub-scale stocks stay stored but out of the list.

Run from the backend folder:
    cd C:\\rush-algo-fixed\\backend
    python rescore_universe.py
"""
import json
import os
import tempfile

from config import settings
from data.fundamental import (
    _parse_indianapi, CACHE_FILE, SYMBOL_CACHE_FILE,
)
from data.fundamental_engine import compute_fundamental_score
from models.schemas import FundamentalData

_FIELDS = set(getattr(FundamentalData, "model_fields", None)
              or getattr(FundamentalData, "__fields__", {}))


def _atomic_write(path, obj):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def main():
    print("Re-scoring universe with the six-category engine from STORED raw data "
          "(no API calls)...\n")

    if not os.path.exists(SYMBOL_CACHE_FILE):
        print("No per-symbol cache found — nothing to re-score."); return

    with open(SYMBOL_CACHE_FILE, encoding="utf-8") as f:
        by_symbol = json.load(f)

    rescored = skipped = 0
    for sym, rec in by_symbol.items():
        raw = rec.get("_raw")
        if not raw:
            skipped += 1
            continue                       # can't re-score without stored raw
        try:
            # re-extract absolute fields with the current parser
            fd = _parse_indianapi(sym, raw)
            if fd:
                for k in ("revenue_cr", "net_income_cr", "operating_income_cr",
                          "fcf_cr", "revenue_growth_calc", "profit_growth_calc",
                          "roe", "pe_ratio", "debt_to_equity", "promoter_holding",
                          "market_cap_cr", "current_ratio"):
                    if hasattr(fd, k):
                        rec[k] = getattr(fd, k)
            # re-run the engine
            bd = compute_fundamental_score(raw)
            rec["_breakdown"] = bd
            rec["score"] = bd["score"]
            rescored += 1
        except Exception as exc:
            print(f"  skip {sym}: {exc}")
            skipped += 1

    _atomic_write(SYMBOL_CACHE_FILE, by_symbol)
    print(f"Per-symbol cache: re-scored {rescored}, skipped {skipped} (no raw).")

    # Rebuild assembled universe file: enriched stocks, slim fields, sorted, with the
    # revenue-scale shortlist floor applied.
    min_rev = getattr(settings, "MIN_REVENUE_CR", 0.0)
    enriched = [v for v in by_symbol.values() if v.get("roe") is not None]
    # Shortlist requires a REAL revenue at/above floor. Null/zero revenue (shells,
    # data gaps) is excluded from the list — still stored & searchable in Deep Dives.
    shortlist = [v for v in enriched
                 if v.get("revenue_cr") is not None and v.get("revenue_cr") >= min_rev]
    shortlist.sort(key=lambda r: r.get("score", 0), reverse=True)
    slim = [{k: v for k, v in r.items() if k in _FIELDS} for r in shortlist]
    _atomic_write(CACHE_FILE, slim)

    excluded = len(enriched) - len(shortlist)
    print(f"Universe file rebuilt: {len(slim)} shortlisted "
          f"(excluded {excluded} below ₹{min_rev:.0f} Cr revenue, still stored).\n")

    print("New Top 15 (revenue ≥ floor):")
    for r in shortlist[:15]:
        print(f"  {r.get('symbol'):14s} {r.get('score'):5}  rev ₹{r.get('revenue_cr')} Cr")

    for tag in ("MMTC", "ATLANTAELE"):
        hit = next((r for r in enriched if str(r.get("symbol","")).upper()==tag), None)
        if hit:
            inlist = "in shortlist" if (hit.get("revenue_cr") or 0) >= min_rev else "EXCLUDED (sub-scale)"
            print(f"\n  {tag}: score {hit.get('score')}  rev ₹{hit.get('revenue_cr')} Cr  [{inlist}]")

    print("\nDone. Restart the backend or refresh the dashboard to see the new ranking.")


if __name__ == "__main__":
    main()
