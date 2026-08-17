"""
Rush Algo - Sync Universe to Zerodha-verified symbols
=====================================================
Cleans data_cache/fundamental_universe.json so it contains ONLY symbols that:
  1. match a Zerodha NSE EQ instrument exactly, AND
  2. are NOT in the excluded list (SEBI action / insolvent / history-broken)

SAFE BY DESIGN:
  - Backs up the original to fundamental_universe.json.backup BEFORE writing.
  - Preserves the original JSON structure (dict-of-symbols OR list).
  - If it cannot confidently parse the file's shape, it ABORTS without writing.

USAGE (needs valid ZERODHA token in .env, or the cached instrument file present):
  python sync_universe.py            # dry run - shows what WOULD change, writes nothing
  python sync_universe.py --apply    # actually back up + overwrite the universe file
"""
from __future__ import annotations
import json
import os
import sys
import shutil

UNIVERSE_PATH = os.path.join("data_cache", "fundamental_universe.json")
BACKUP_PATH   = os.path.join("data_cache", "fundamental_universe.json.backup")
INSTR_CACHE   = os.path.join("data_cache", "kite_instruments_nse.json")


def load_zerodha_eq() -> set:
    """Set of Zerodha NSE EQ tradingsymbols. Uses cached instrument file if present,
    else fetches fresh via KiteData."""
    # Prefer the cached instrument map (already built by KiteData)
    if os.path.exists(INSTR_CACHE):
        try:
            with open(INSTR_CACHE, encoding="utf-8") as f:
                return set(k.upper() for k in json.load(f).keys())
        except Exception:
            pass
    # Fallback: fetch live
    from brokers.zerodha_client import _kite
    kc = _kite(with_token=True)
    rows = kc.instruments("NSE")
    return set(str(r.get("tradingsymbol", "")).upper().strip()
               for r in rows if str(r.get("instrument_type", "")).upper() == "EQ")


def get_excluded() -> set:
    try:
        from data.excluded_symbols import EXCLUDED_SYMBOLS
        return set(k.upper() for k in EXCLUDED_SYMBOLS.keys())
    except Exception:
        return set()


def symbol_of(item):
    """Extract a symbol from a universe entry (str or dict)."""
    if isinstance(item, str):
        return item.upper().strip()
    if isinstance(item, dict):
        s = item.get("symbol") or item.get("ticker") or item.get("name")
        return str(s).upper().strip() if s else None
    return None


def main():
    apply = "--apply" in sys.argv

    if not os.path.exists(UNIVERSE_PATH):
        print(f"ERROR: {UNIVERSE_PATH} not found. Run from C:\\rush-algo-fixed\\backend")
        sys.exit(1)

    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    eq = load_zerodha_eq()
    excluded = get_excluded()
    print(f"Zerodha EQ symbols: {len(eq)}")
    print(f"Excluded symbols:   {len(excluded)}")
    print(f"Universe file shape: {type(data).__name__}\n")

    kept, dropped_excluded, dropped_nomatch = [], [], []

    def verdict(sym):
        if sym is None:
            return "nomatch"
        if sym in excluded:
            return "excluded"
        if sym in eq:
            return "keep"
        return "nomatch"

    # -- Handle the two common shapes, preserving structure --------------------
    new_data = None

    if isinstance(data, dict):
        # Could be {symbol: {...}} OR {"stocks":[...]} etc.
        wrapper_key = None
        for key in ("stocks", "universe", "data", "symbols"):
            if key in data and isinstance(data[key], list):
                wrapper_key = key
                break

        if wrapper_key:
            items = data[wrapper_key]
            new_items = []
            for it in items:
                sym = symbol_of(it)
                v = verdict(sym)
                if v == "keep":
                    kept.append(sym); new_items.append(it)
                elif v == "excluded":
                    dropped_excluded.append(sym)
                else:
                    dropped_nomatch.append(sym or "<no symbol>")
            new_data = dict(data)
            new_data[wrapper_key] = new_items
        else:
            # plain {symbol: {...}} mapping
            new_map = {}
            for k, val in data.items():
                sym = str(k).upper().strip()
                v = verdict(sym)
                if v == "keep":
                    kept.append(sym); new_map[k] = val
                elif v == "excluded":
                    dropped_excluded.append(sym)
                else:
                    dropped_nomatch.append(sym)
            new_data = new_map

    elif isinstance(data, list):
        new_items = []
        for it in data:
            sym = symbol_of(it)
            v = verdict(sym)
            if v == "keep":
                kept.append(sym); new_items.append(it)
            elif v == "excluded":
                dropped_excluded.append(sym)
            else:
                dropped_nomatch.append(sym or "<no symbol>")
        new_data = new_items

    else:
        print(f"ABORT: unexpected JSON shape ({type(data).__name__}). Nothing written.")
        print("Paste me the first ~20 lines of the file and I'll adjust the script.")
        sys.exit(1)

    # -- Summary ---------------------------------------------------------------
    print("=" * 68)
    print(f"KEEP (Zerodha EQ, not excluded): {len(kept)}")
    print(f"DROP - excluded (SEBI/insolvent/history-broken): {len(dropped_excluded)}")
    print(f"DROP - no Zerodha EQ match (SME/BE/delisted): {len(dropped_nomatch)}")
    print("=" * 68)
    if dropped_excluded:
        print("\nDropped (excluded):")
        print("  " + ", ".join(sorted(dropped_excluded)))
    if dropped_nomatch:
        print(f"\nDropped (no match) - first 40 of {len(dropped_nomatch)}:")
        print("  " + ", ".join(sorted(dropped_nomatch)[:40]) + (" ..." if len(dropped_nomatch) > 40 else ""))

    if not apply:
        print("\n--- DRY RUN. Nothing written. ---")
        print("Re-run with  --apply  to back up and overwrite the universe file:")
        print("    python sync_universe.py --apply")
        return

    # -- Apply: back up, then overwrite ----------------------------------------
    if len(kept) < 100:
        print(f"\nABORT: only {len(kept)} symbols would remain - that looks wrong. "
              "Not overwriting. Check the file / token.")
        sys.exit(1)

    shutil.copy2(UNIVERSE_PATH, BACKUP_PATH)
    print(f"\nBacked up original -> {BACKUP_PATH}")
    with open(UNIVERSE_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    print(f"WROTE cleaned universe -> {UNIVERSE_PATH}  ({len(kept)} symbols)")
    print("\nDone. Restart the backend to load the cleaned universe.")


if __name__ == "__main__":
    main()
