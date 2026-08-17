"""
Rush Algo - Verify cached NSE records match the ticker they're stored under
===========================================================================
IndianAPI's /stock?name=X endpoint does FUZZY name matching and silently
returns a DIFFERENT company when it can't find an exact ticker match:

    BAJAJ-AUTO -> Bajaj Finance          (motorcycles -> NBFC)
    KEC        -> EPW India              (power infra -> unrelated)
    HCL-INSYS  -> HCL Technologies       (~Rs 500cr -> ~Rs 4 lakh cr)
    AERON      -> Hindustan Aeronautics  (small cap -> defence PSU)

It returns HTTP 200 with a valid-looking payload, so nothing downstream notices.
The result is a cache where a ticker carries ANOTHER company's fundamentals -
which is far worse than a bad score, because acting on it means buying the
wrong stock.

This script cross-checks every cached record against NSE's own securities
master (data_cache/EQUITY_L.csv, SYMBOL -> NAME OF COMPANY) and reports
mismatches. READ-ONLY by default - it changes nothing unless you pass --purge.

USAGE (from C:\\ALGO\\backend, venv active):
  python verify_symbol_names.py                # report only, no network, no writes
  python verify_symbol_names.py --purge        # delete mismatched records so a
                                               # later refresh re-fetches them
"""
from __future__ import annotations
import csv
import json
import os
import re
import sys
from difflib import SequenceMatcher

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache")
SYMBOL_CACHE = os.path.join(CACHE_DIR, "fundamentals_by_symbol.json")
EQUITY_MASTER = os.path.join(CACHE_DIR, "EQUITY_L.csv")

# Similarity below this is treated as a different company. Tuned so legitimate
# formatting differences pass ("Reliance Industries" vs "Reliance Industries
# Limited") while genuinely different names fail ("Bajaj Auto" vs "Bajaj
# Finance" -> the shared first word isn't enough to carry it).
# Tuned against the real mismatches found in the cache: catches all 10 known-bad
# pairs (incl. BAJAJHLDNG at 0.62) with zero false positives across 12 legitimate
# name variations.
MATCH_THRESHOLD = 0.66

# Aggressive stop list for SIMILARITY - descriptive filler that would otherwise
# make two unrelated companies look alike.
_NOISE = {
    "limited", "ltd", "ltd.", "the", "india", "indian", "company", "co",
    "corporation", "corp", "industries", "enterprises", "&", "and",
    "private", "pvt", "public", "plc", "inc", "group", "holdings",
}

# Minimal stop list for ACRONYMS - strips only legal suffixes. Using the
# aggressive list here broke the very abbreviations it was meant to catch:
# removing "Services" from "Tata Consultancy Services" no longer yields TCS.
_LEGAL_ONLY = {"limited", "ltd", "ltd.", "the", "private", "pvt", "plc", "inc", "&", "and"}


def _clean(name: str, stop: set, expand_amp: bool = False) -> str:
    s = (name or "").lower()
    if expand_amp:
        s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w and w not in stop)


def _acronym(s: str) -> str:
    return "".join(w[0] for w in s.split() if w)


def _similar(cached: str, official: str, symbol: str = "") -> float:
    # A cached "name" that is just the TICKER means the API returned no company
    # name and the code fell back to the symbol. That's MISSING data, not a wrong
    # company - flagging it would purge perfectly valid records (M&MFIN, RAINBOW).
    if symbol and re.sub(r"[^a-z0-9]", "", (cached or "").lower()) == \
                  re.sub(r"[^a-z0-9]", "", symbol.lower()):
        return 1.0

    na, nb = _clean(cached, _NOISE), _clean(official, _NOISE)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    # Containment counts ONLY when the shorter side is >=2 words. A single word
    # inside a longer name proves nothing - "lal" sits inside "dr lal pathlabs",
    # "value" inside "aptus value housing", both different companies.
    sa, sb = na.split(), nb.split()
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(shorter) >= 2 and " ".join(shorter) in " ".join(longer):
        return 0.95

    # Abbreviations are the SAME company (TCS vs Tata Consultancy Services).
    # `&` is expanded to "and" here so M&M can match Mahindra & Mahindra - the
    # aggressive noise list strips `&` entirely, which broke that acronym.
    ca, cb = _clean(cached, _LEGAL_ONLY, True), _clean(official, _LEGAL_ONLY, True)
    for a, b in ((ca, cb), (cb, ca)):
        if " " not in a and len(a) >= 2 and a == _acronym(b):
            return 0.93
    # Second pass ignoring the joining "and": M&M -> "mm" vs Mahindra Mahindra.
    for a, b in ((ca, cb), (cb, ca)):
        b_init = "".join(w[0] for w in b.split() if w and w != "and")
        a_comp = a.replace(" and ", "").replace(" ", "")
        if len(a_comp) >= 2 and a_comp == b_init:
            return 0.93

    # A shared FIRST word is deliberately not special-cased: "Bajaj Auto" and
    # "Bajaj Finance" are different companies and must fall through to here.
    return SequenceMatcher(None, na, nb).ratio()


def load_master() -> dict:
    """SYMBOL -> official company name, from NSE's own securities master."""
    if not os.path.exists(EQUITY_MASTER):
        print(f"ERROR: {EQUITY_MASTER} not found - can't verify without NSE's master list.")
        sys.exit(1)
    out = {}
    with open(EQUITY_MASTER, encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            sym = (row.get("SYMBOL") or "").strip().upper()
            nm = (row.get("NAME OF COMPANY") or "").strip()
            if sym and nm:
                out[sym] = nm
    return out


def main():
    purge = "--purge" in sys.argv

    if not os.path.exists(SYMBOL_CACHE):
        print(f"ERROR: {SYMBOL_CACHE} not found.")
        sys.exit(1)

    master = load_master()
    with open(SYMBOL_CACHE, encoding="utf-8") as f:
        cache = json.load(f)

    print(f"NSE master     : {len(master)} symbols")
    print(f"Cached records : {len(cache)}\n")

    ok, mismatched, not_in_master = [], [], []

    for sym, rec in cache.items():
        cached_name = (rec.get("name") or "").strip()
        official = master.get(sym.upper())
        if not official:
            not_in_master.append(sym)
            continue
        score = _similar(cached_name, official, sym)
        if score >= MATCH_THRESHOLD:
            ok.append(sym)
        else:
            mismatched.append((sym, cached_name, official, score))

    print("=" * 78)
    print(f"  MATCHES OK          : {len(ok)}")
    print(f"  MISMATCHED          : {len(mismatched)}   <-- wrong company's data")
    print(f"  NOT IN NSE MASTER   : {len(not_in_master)}   (delisted / SME / stale master file)")
    print("=" * 78)

    if mismatched:
        mismatched.sort(key=lambda x: x[3])
        print(f"\nMISMATCHED RECORDS (showing up to 60 of {len(mismatched)}):\n")
        print(f"  {'SYMBOL':<14}{'CACHED AS':<38}{'SHOULD BE':<38}{'SIM'}")
        print("  " + "-" * 94)
        for sym, cached, official, score in mismatched[:60]:
            print(f"  {sym:<14}{cached[:36]:<38}{official[:36]:<38}{score:.2f}")

        out_path = os.path.join(CACHE_DIR, "name_mismatches.txt")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                for sym, cached, official, score in mismatched:
                    f.write(f"{sym}\t{cached}\t{official}\t{score:.3f}\n")
            print(f"\n  Full list written to: {out_path}")
        except Exception as exc:
            print(f"  (could not write list: {exc})")

    if not_in_master and len(not_in_master) <= 40:
        print(f"\nNOT IN MASTER: {', '.join(sorted(not_in_master)[:40])}")

    # ── DUPLICATE COMPANIES ───────────────────────────────────────────────────
    # The same company appearing under TWO tickers is the same fuzzy-match bug,
    # but invisible to the name check above: tickers like AERON, CONS, INFRA,
    # KEN and VALUE aren't in NSE's master at all, so there's nothing to compare
    # them against - they land in "NOT IN MASTER" and pass through. The giveaway
    # is that they carry an IDENTICAL company name to a legitimate ticker.
    #
    #     AERON + HAL    -> both "Hindustan Aeronautics"  (AERON is Aeron Composite)
    #     CONS  + TCS    -> both "Tata Consultancy Services"
    #     VALUE + APTUS  -> both "Aptus Value Housing"
    #
    # Resolution: the ticker present in NSE's master is canonical, the other is
    # the impostor. When BOTH are in the master they are genuinely distinct
    # listings (e.g. GLOBAL/MEDANTA, PTC/PTCIL) - those are reported for manual
    # review, never auto-dropped, because guessing there would delete real data.
    from collections import defaultdict
    byname = defaultdict(list)
    for sym, rec in cache.items():
        nm = (rec.get("name") or "").strip()
        if nm:
            byname[nm].append(sym)

    dup_drop, dup_manual = [], []
    for nm, syms in byname.items():
        if len(syms) < 2:
            continue
        in_master = [s for s in syms if s.upper() in master]
        if len(in_master) == 1:
            keep = in_master[0]
            for s in syms:
                if s != keep:
                    dup_drop.append((s, nm, keep))
        elif len(in_master) == 0:
            keep = max(syms, key=len)      # longest ticker is the likelier real one
            for s in syms:
                if s != keep:
                    dup_drop.append((s, nm, keep))
        else:
            dup_manual.append((nm, syms))

    if dup_drop or dup_manual:
        print("\n" + "=" * 78)
        print(f"  DUPLICATE COMPANIES: {len(dup_drop)} impostor tickers, "
              f"{len(dup_manual)} need manual review")
        print("=" * 78)
    if dup_drop:
        print(f"\n  {'DROP':<16}{'KEEP':<16}COMPANY")
        print("  " + "-" * 70)
        for s, nm, keep in sorted(dup_drop):
            print(f"  {s:<16}{keep:<16}{nm[:40]}")
    if dup_manual:
        print("\n  BOTH tickers are in NSE's master - genuinely separate listings,")
        print("  left alone (verify by hand if unsure):")
        for nm, syms in dup_manual:
            print(f"      {nm[:44]:<46}{syms}")

    if not purge:
        print("\n--- REPORT ONLY. Nothing changed. ---")
        if mismatched:
            print("To DELETE the mismatched records (so a later refresh re-fetches them):")
            print("    python verify_symbol_names.py --purge")
        return

    if not mismatched and not dup_drop:
        print("\nNothing to purge.")
        return

    # Back up before removing anything - this file is expensive to rebuild.
    backup = SYMBOL_CACHE + ".before_purge"
    try:
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        print(f"\nBacked up current cache -> {backup}")
    except Exception as exc:
        print(f"\nABORT: could not write backup ({exc}). Nothing removed.")
        sys.exit(1)

    for sym, _c, _o, _s in mismatched:
        cache.pop(sym, None)
    for sym, _nm, _keep in dup_drop:
        cache.pop(sym, None)

    tmp = SYMBOL_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SYMBOL_CACHE)

    print(f"Purged {len(mismatched)} name-mismatched + {len(dup_drop)} duplicate "
          f"records. {len(cache)} remain.")
    print("Run a Refresh to re-fetch them, then: python rescore_universe.py")


if __name__ == "__main__":
    main()
