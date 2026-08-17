"""
Rush Algo — Excluded Symbols (data-integrity guard)
===================================================
Symbols that must NEVER be traded or backtested, with the verified reason.

WHY A DROP-LIST: these came out of a full universe-vs-Zerodha audit. They fall in
three buckets, none of which are cleanly tradeable:
  1) SEBI action / insolvency  -> uninvestable regardless of price
  2) Illiquid distressed micro-caps -> costs destroy any edge (tiny cap, ~hundreds
     of shares/day volume); your MIN_MARKET_CAP_CR filter should catch these too
  3) Merger/demerger -> the stock may still trade, but its PRICE HISTORY IS BROKEN
     across the corporate-action date, so backtests would show a fake gap/return

This is enforced at the DATA layer (KiteData._token) so an excluded symbol can't be
fetched — which blocks it from BOTH backtesting and live trading in one place.

To re-include something later (e.g. a demerged name after enough clean post-event
history accumulates), just delete its line here. Add new ones as corporate actions
happen. Keep the reason — future-you will want to know why.
"""
from __future__ import annotations

# symbol -> reason (reason is for humans; code only checks membership)
EXCLUDED_SYMBOLS = {
    # --- SEBI action / insolvency ---
    "GENSOL":     "SEBI ban (fund diversion / fraud), forensic audit, ESM",
    "AGSTRA":     "CIRP (insolvency); CRISIL 'D'; cap ~28Cr",
    "RELINFRA":   "under NSE surveillance series (not clean EQ)",

    # --- illiquid distressed micro-caps (fail quality/liquidity bar) ---
    "TULSYAN":    "negative earnings; cap ~44Cr",
    "LAKSELECON": "cap ~10Cr; ~650 shares/day volume",
    "KHATJUNKER": "obscure illiquid micro-cap",
    "BECLCIND":   "obscure illiquid micro-cap",
    "STCKKRETAIL":"obscure illiquid micro-cap",
    "BEONIDA":    "obscure illiquid micro-cap",
    "BETTL":      "obscure illiquid micro-cap",
    "JUMBO":      "obscure illiquid micro-cap",
    "LANCER":     "obscure illiquid micro-cap",
    "STSIMCA":    "obscure illiquid micro-cap",
    "SAKTHIFIN":  "obscure illiquid micro-cap",

    # --- merger / demerger: PRICE HISTORY BROKEN across the event ---
    # (These may trade under a new symbol, but pre/post series aren't comparable,
    #  so they are NOT clean for multi-year backtests. Re-add later if desired.)
    "TATAMOTORS": "demerged into TMPV + CV entity (Oct 2025); history broken",
    "GUJGASLTD":  "renamed GUJENERGY after GSPC/GSPL merger (Jul 2026); history broken",
}


def is_excluded(symbol: str) -> bool:
    s = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
    return s in EXCLUDED_SYMBOLS


def exclusion_reason(symbol: str) -> str:
    s = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
    return EXCLUDED_SYMBOLS.get(s, "")
