"""
Verify the ENTIRE fundamental universe against Zerodha's NSE instrument master.

Reads every symbol from data_cache/fundamental_universe.json, checks each against
Zerodha's live instrument list, and reports:
  - how many resolve as tradable NSE equities (EQ)
  - the full list of MISSING symbols (renames, demergers, delistings, SME, etc.)
  - close-match hints for misses so you can see the real Zerodha symbol
  - writes the missing list to  data_cache/zerodha_missing_symbols.txt

USAGE (needs a valid ZERODHA token in .env):
  python verify_universe.py
"""
from __future__ import annotations
import json
import os

from brokers.zerodha_client import _kite

UNIVERSE_PATH = os.path.join("data_cache", "fundamental_universe.json")
MISSING_OUT   = os.path.join("data_cache", "zerodha_missing_symbols.txt")


def load_universe_symbols() -> list:
    """Pull the symbol list out of fundamental_universe.json, whatever its shape."""
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    syms = []
    # Try the common shapes defensively — dict of {symbol: {...}}, list of dicts,
    # list of strings, or {"stocks": [...]} / {"universe": [...]}.
    if isinstance(data, dict):
        # {"stocks": [...]} or {"universe": [...]} or {"data": [...]}
        for key in ("stocks", "universe", "data", "symbols"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            # plain {symbol: {...}} mapping
            syms = [str(k).upper().strip() for k in data.keys()]
            return sorted(set(syms))

    if isinstance(data, list):
        for row in data:
            if isinstance(row, str):
                syms.append(row.upper().strip())
            elif isinstance(row, dict):
                s = row.get("symbol") or row.get("ticker") or row.get("name")
                if s:
                    syms.append(str(s).upper().strip())
    return sorted(set(s for s in syms if s))


def main():
    if not os.path.exists(UNIVERSE_PATH):
        print(f"ERROR: {UNIVERSE_PATH} not found. Run from C:\\rush-algo-fixed\\backend")
        return

    symbols = load_universe_symbols()
    print(f"Universe file: {UNIVERSE_PATH}")
    print(f"Symbols loaded: {len(symbols)}\n")
    if not symbols:
        print("Could not extract symbols — the JSON shape is unexpected. Paste me the")
        print("first ~20 lines of fundamental_universe.json and I'll adjust the parser.")
        return

    kc = _kite(with_token=True)
    rows = kc.instruments("NSE")
    eq = set()
    all_syms = {}
    for r in rows:
        s = str(r.get("tradingsymbol", "")).upper().strip()
        t = str(r.get("instrument_type", "")).upper()
        all_syms[s] = t
        if t == "EQ":
            eq.add(s)
    print(f"Zerodha NSE master: {len(rows)} rows, {len(eq)} EQ symbols\n")

    found, missing = [], []
    for s in symbols:
        if s in eq:
            found.append(s)
        else:
            missing.append(s)

    print("=" * 70)
    print(f"RESULT: {len(found)}/{len(symbols)} resolve as tradable NSE equities")
    print(f"        {len(missing)} MISSING")
    print("=" * 70)

    if missing:
        print(f"\nMISSING SYMBOLS ({len(missing)}) — with close-match hints:\n")
        for s in missing:
            root = s[:5]
            near = [k for k in all_syms if root and root in k][:5]
            if s in all_syms:
                hint = f"exists but type={all_syms[s]} (NOT EQ — likely index/ETF/SME)"
            elif near:
                hint = "maybe: " + ", ".join(f"{k}[{all_syms[k]}]" for k in near)
            else:
                hint = "no close match (delisted / renamed / demerged)"
            print(f"  {s:<16}{hint}")

        try:
            with open(MISSING_OUT, "w", encoding="utf-8") as f:
                f.write("\n".join(missing))
            print(f"\nMissing list written to: {MISSING_OUT}")
        except Exception as exc:
            print(f"\n(could not write missing list: {exc})")

    pct = len(found) / len(symbols) * 100 if symbols else 0
    print(f"\nCOVERAGE: {pct:.1f}% of your universe is tradable/fetchable on Zerodha.")
    print()


if __name__ == "__main__":
    main()
