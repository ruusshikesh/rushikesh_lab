"""
Market-cap segmentation exporter for Rush Algo.

Reads the rescored universe (data_cache/fundamentals_by_symbol.json) and produces
FOUR separately-ranked lists so like competes with like — a ₹305 Cr micro-cap no
longer out-ranks a ₹10,000 Cr mid-cap on the same board.

Buckets (Indian-market realistic):
    LARGE  : market cap  > ₹20,000 Cr
    MID    : ₹5,000 – 20,000 Cr
    SMALL  : ₹1,000 – 5,000 Cr
    MICRO  : < ₹1,000 Cr

Shortlist rules match rescore_universe: roe present AND revenue ≥ MIN_REVENUE_CR.
Stocks with no market cap are put in an UNKNOWN bucket (still visible, not dropped).

Run from backend:  python segment_universe.py
Outputs: ranked_large.txt, ranked_mid.txt, ranked_small.txt, ranked_micro.txt
         (+ ranked_segmented.txt with all four in one file)
"""
import json, os

CACHE = "data_cache/fundamentals_by_symbol.json"

# bucket boundaries in ₹ crore
LARGE_MIN = 20000.0
MID_MIN   = 5000.0
SMALL_MIN = 1000.0

try:
    from config import settings                     # same import rescore_universe uses
    MIN_REV = getattr(settings, "MIN_REVENUE_CR", 100.0)
except Exception:
    MIN_REV = 100.0

def bucket(mc):
    if mc is None:
        return "UNKNOWN"
    if mc > LARGE_MIN:  return "LARGE"
    if mc >= MID_MIN:   return "MID"
    if mc >= SMALL_MIN: return "SMALL"
    return "MICRO"

def main():
    with open(CACHE, encoding="utf-8") as f:
        d = json.load(f)

    # shortlist: same rules as rescore (enriched + real revenue at/above floor)
    rows = []
    for v in d.values():
        if v.get("roe") is None:
            continue
        rev = v.get("revenue_cr")
        if rev is None or rev < MIN_REV:
            continue
        rows.append(v)

    buckets = {"LARGE": [], "MID": [], "SMALL": [], "MICRO": [], "UNKNOWN": []}
    for r in rows:
        buckets[bucket(r.get("market_cap_cr"))].append(r)

    for b in buckets.values():
        b.sort(key=lambda r: r.get("score", 0) or 0, reverse=True)

    def fmt_row(i, r):
        mc = r.get("market_cap_cr")
        mc_s = f"₹{mc:,.0f}Cr" if mc is not None else "—"
        return (f"{i:>4}  {r.get('symbol',''):14s} {str(r.get('score','')):>5}  "
                f"{mc_s:>15}  roe={r.get('roe')}  rev=₹{r.get('revenue_cr')}Cr  "
                f"{r.get('name','')[:40]}")

    labels = [
        ("LARGE", "LARGE CAP  ( > ₹20,000 Cr )"),
        ("MID",   "MID CAP    ( ₹5,000 – 20,000 Cr )"),
        ("SMALL", "SMALL CAP  ( ₹1,000 – 5,000 Cr )"),
        ("MICRO", "MICRO CAP  ( < ₹1,000 Cr )"),
    ]

    # per-bucket files
    fname = {"LARGE":"ranked_large.txt","MID":"ranked_mid.txt",
             "SMALL":"ranked_small.txt","MICRO":"ranked_micro.txt"}
    for key, title in labels:
        with open(fname[key], "w", encoding="utf-8") as f:
            f.write(f"{title}   —   {len(buckets[key])} stocks\n")
            f.write("="*100 + "\n")
            for i, r in enumerate(buckets[key], 1):
                f.write(fmt_row(i, r) + "\n")

    # combined file
    with open("ranked_segmented.txt", "w", encoding="utf-8") as f:
        for key, title in labels:
            f.write(f"\n{title}   —   {len(buckets[key])} stocks\n")
            f.write("="*100 + "\n")
            for i, r in enumerate(buckets[key], 1):
                f.write(fmt_row(i, r) + "\n")
        if buckets["UNKNOWN"]:
            f.write(f"\nUNKNOWN MARKET CAP   —   {len(buckets['UNKNOWN'])} stocks\n")
            f.write("="*100 + "\n")
            for i, r in enumerate(buckets["UNKNOWN"], 1):
                f.write(fmt_row(i, r) + "\n")

    # console summary
    print("Segmentation complete. Counts per bucket:")
    for key, title in labels:
        print(f"  {title:38s}: {len(buckets[key]):4d}")
    if buckets["UNKNOWN"]:
        print(f"  {'UNKNOWN market cap':38s}: {len(buckets['UNKNOWN']):4d}")
    print("\nTop 10 in each bucket:")
    for key, title in labels:
        print(f"\n{title}")
        for i, r in enumerate(buckets[key][:10], 1):
            mc = r.get("market_cap_cr")
            mc_s = f"₹{mc:,.0f}Cr" if mc is not None else "—"
            print(f"  {i:>3}. {r.get('symbol',''):14s} {str(r.get('score','')):>5}  {mc_s:>14}")
    print("\nFiles written: ranked_large.txt, ranked_mid.txt, ranked_small.txt, "
          "ranked_micro.txt, ranked_segmented.txt")

if __name__ == "__main__":
    main()
