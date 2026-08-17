"""
StrykeX — Dhan Broker Client
FIX: Added get_open_orders() and close_order() required by scanner.py
FIX: Symbol→security_id now resolved from Dhan's full instrument master CSV
     (covers all NSE equities), not a 15-symbol hardcoded list.
Token valid 30 days — generate at https://web.dhan.co/
"""
from __future__ import annotations
import csv
import io
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import requests

from config import settings
from models.schemas import Order, OrderStatus

logger = logging.getLogger(__name__)

EXCHANGE_NSE  = "NSE_EQ"
PRODUCT_INTRA = "INTRADAY"
PRODUCT_CNC   = "CNC"        # delivery — for POSITIONAL trades (not auto-squared-off)
ORDER_LIMIT   = "LIMIT"
ORDER_MARKET  = "MARKET"
ORDER_SLM     = "STOP_LOSS_MARKET"

# Dhan's public instrument master (no auth required). The "detailed" variant has
# clearer column names; we fall back to the compact one if needed.
DHAN_SCRIP_URLS = [
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
    "https://images.dhan.co/api-data/api-scrip-master.csv",
]
_SCRIP_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data_cache", "dhan_scrip_master.csv",
)


class DhanClient:
    # Emergency fallback ONLY — used if the instrument master can't be downloaded.
    # Normal operation builds a full NSE map from the CSV (see _load_symbol_map).
    _FALLBACK_IDS: Dict[str, str] = {
        "RELIANCE": "2885", "TCS": "11536", "INFY": "1594", "HDFCBANK": "1333",
        "ICICIBANK": "4963", "SBIN": "3045", "ITC": "1660", "LT": "11483",
    }

    # Class-level cache so the (large) symbol map is built once per process,
    # shared across all DhanClient instances.
    _symbol_map: Optional[Dict[str, str]] = None

    def __init__(self):
        self._dhan = None
        self._local_orders: dict = {}
        self._init()

    def _init(self):
        if not settings.DHAN_ACCESS_TOKEN:
            logger.warning("DHAN_ACCESS_TOKEN not set")
            return
        try:
            from dhanhq import dhanhq
            self._dhan = dhanhq(
                client_id=settings.DHAN_CLIENT_ID,
                access_token=settings.DHAN_ACCESS_TOKEN,
            )
            logger.info("Dhan client ready (client_id=%s)", settings.DHAN_CLIENT_ID)
        except ImportError:
            logger.error("dhanhq not installed: pip install dhanhq")

    # ── Instrument master → symbol map ────────────────────────────────────────
    @classmethod
    def _load_symbol_map(cls) -> Dict[str, str]:
        """
        Build {NSE_SYMBOL: security_id} from Dhan's instrument master CSV.
        Downloads once, caches to disk, and reuses the cached copy thereafter.
        Filters to NSE equity rows. Falls back to a tiny built-in map on failure.
        """
        if cls._symbol_map is not None:
            return cls._symbol_map

        raw = cls._read_scrip_csv()
        if not raw:
            logger.warning("Dhan: using fallback symbol map (%d symbols) — full "
                           "instrument master unavailable", len(cls._FALLBACK_IDS))
            cls._symbol_map = dict(cls._FALLBACK_IDS)
            return cls._symbol_map

        mapping = cls._parse_scrip_csv(raw)
        if len(mapping) < 50:   # parse clearly went wrong; don't trust it
            logger.warning("Dhan: instrument master parsed only %d symbols — using "
                           "fallback map instead", len(mapping))
            cls._symbol_map = dict(cls._FALLBACK_IDS)
        else:
            logger.info("Dhan: instrument master loaded — %d NSE symbols mapped", len(mapping))
            cls._symbol_map = mapping
        return cls._symbol_map

    @staticmethod
    def _read_scrip_csv() -> str:
        # Prefer a fresh-enough disk cache
        try:
            if os.path.exists(_SCRIP_CACHE) and os.path.getsize(_SCRIP_CACHE) > 100_000:
                with open(_SCRIP_CACHE, encoding="utf-8", errors="ignore") as f:
                    return f.read()
        except Exception:
            pass
        # Otherwise download and cache
        for url in DHAN_SCRIP_URLS:
            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200 and len(resp.text) > 100_000:
                    try:
                        os.makedirs(os.path.dirname(_SCRIP_CACHE), exist_ok=True)
                        with open(_SCRIP_CACHE, "w", encoding="utf-8") as f:
                            f.write(resp.text)
                    except Exception as exc:
                        logger.debug("Dhan scrip cache write failed: %s", exc)
                    return resp.text
            except Exception as exc:
                logger.debug("Dhan scrip download failed (%s): %s", url, exc)
        return ""

    @staticmethod
    def _parse_scrip_csv(raw: str) -> Dict[str, str]:
        """Parse the CSV defensively — column names differ between CSV variants."""
        mapping: Dict[str, str] = {}
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            return mapping

        # Resolve the columns we need by fuzzy-matching header names
        def find_col(*needles):
            for col in reader.fieldnames:
                low = col.lower().replace("_", "").replace(" ", "")
                if all(n in low for n in needles):
                    return col
            return None

        c_secid  = find_col("security", "id") or find_col("securityid")
        c_symbol = (find_col("trading", "symbol") or find_col("custom", "symbol")
                    or find_col("symbol"))
        c_exch   = find_col("exch") or find_col("exchange")
        c_seg    = find_col("segment")
        c_instr  = find_col("instrument")

        if not (c_secid and c_symbol):
            return mapping

        for row in reader:
            try:
                exch = (row.get(c_exch, "") or "").upper()
                if c_exch and "NSE" not in exch:
                    continue
                # keep equities only when we can tell
                seg = (row.get(c_seg, "") or "").upper()
                instr = (row.get(c_instr, "") or "").upper()
                blob = seg + instr
                if blob and not any(k in blob for k in ("EQ", "EQUITY")):
                    continue

                sym = (row.get(c_symbol, "") or "").upper().strip()
                sym = sym.replace("-EQ", "").replace(".NS", "").strip()
                sid = (row.get(c_secid, "") or "").strip()
                if sym and sid and sym not in mapping:
                    mapping[sym] = sid
            except Exception:
                continue
        return mapping

    def _sec_id(self, symbol: str) -> str:
        sym = symbol.upper().replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip()
        sid = self._load_symbol_map().get(sym)
        if not sid:
            raise ValueError(
                f"Security ID not found for {sym}. It may not be an NSE equity, or the "
                f"Dhan instrument master didn't load. Delete data_cache/dhan_scrip_master.csv "
                f"to force a re-download."
            )
        return sid

    def place_order(self, order: Order) -> Order:
        if not self._dhan:
            raise RuntimeError("Dhan client not initialised")
        order.id = order.id or str(uuid.uuid4())[:8]
        # FIX (bug C): POSITIONAL trades must use CNC (delivery). The old code
        # hardcoded INTRADAY, so swing positions were auto-squared-off by Dhan at EOD.
        prod = PRODUCT_INTRA if str(getattr(order.trade_type, "value", order.trade_type)) == "INTRADAY" else PRODUCT_CNC
        resp = self._dhan.place_order(
            security_id      = self._sec_id(order.symbol),
            exchange_segment = EXCHANGE_NSE,
            transaction_type = "BUY" if order.side == "BUY" else "SELL",
            quantity         = order.qty,
            order_type       = ORDER_LIMIT if order.order_type == "LIMIT" else ORDER_MARKET,
            product_type     = prod,
            price            = order.price if order.order_type == "LIMIT" else 0,
        )
        if resp.get("status", "").lower() == "success":  # FIX: normalise case
            order.broker_order_id = str(resp.get("data", {}).get("orderId", ""))
            order.status          = OrderStatus.OPEN
            order.entry_time      = datetime.now()
            self._local_orders[order.id] = order
            logger.info("Dhan order: %s × %d @ ₹%.2f | id=%s",
                        order.symbol, order.qty, order.price, order.broker_order_id)
        else:
            order.status = OrderStatus.REJECTED
            logger.error("Dhan order rejected: %s", resp)
        return order

    def place_sl_order(self, symbol: str, qty: int, trigger: float, side: str = "SELL") -> str:
        if not self._dhan: return ""
        try:  # FIX: _sec_id raises ValueError for unknown symbols — catch to avoid scanner crash
            resp = self._dhan.place_order(
                security_id=self._sec_id(symbol), exchange_segment=EXCHANGE_NSE,
                transaction_type=side, quantity=qty,
                order_type=ORDER_SLM, product_type=PRODUCT_INTRA,
                price=0, trigger_price=round(trigger, 2),
            )
            return str(resp.get("data", {}).get("orderId", ""))
        except Exception as exc:
            logger.error("Dhan place_sl_order failed for %s: %s", symbol, exc)
            return ""

    # FIX: Added — scanner needs these
    def get_open_orders(self) -> List[Order]:
        return [o for o in self._local_orders.values() if o.status == OrderStatus.OPEN]

    def close_order(self, order_id: str, exit_price: float, reason: str = "MANUAL") -> Optional[Order]:
        order = self._local_orders.get(order_id)
        if not order or order.status != OrderStatus.OPEN:
            return None

        # FIX (critical): same class of bug as the other live brokers — the exit
        # order must be accepted by Dhan before we mark the position CLOSED.
        # Previously an exception (or unknown security id) was logged and then the
        # order was marked CLOSED with a fabricated P&L regardless, leaving a real
        # position OPEN and unmanaged while the dashboard showed it as exited.
        if not self._dhan:
            logger.error("Dhan close_order: client not initialised — cannot exit %s, leaving OPEN",
                         order.symbol)
            return None

        try:
            # Exit must use the SAME product as entry (can't close CNC with INTRADAY).
            close_prod = PRODUCT_INTRA if str(getattr(order.trade_type, "value", order.trade_type)) == "INTRADAY" else PRODUCT_CNC
            resp = self._dhan.place_order(
                security_id=self._sec_id(order.symbol), exchange_segment=EXCHANGE_NSE,
                transaction_type="SELL" if order.side == "BUY" else "BUY",
                quantity=order.qty, order_type=ORDER_MARKET,
                product_type=close_prod, price=0,
            )
        except Exception as exc:
            logger.error("Dhan exit FAILED for %s (%s) — position kept OPEN for retry",
                         order.symbol, exc)
            return None

        # Dhan returns a status field on the response, mirror place_order()'s check.
        if str(resp.get("status", "")).lower() != "success":
            logger.error("Dhan exit REJECTED for %s: %s — position kept OPEN for retry",
                         order.symbol, resp)
            return None

        order.exit_broker_order_id = str(resp.get("data", {}).get("orderId", ""))
        order.exit_price  = exit_price
        order.exit_time   = datetime.now()
        order.exit_reason = reason
        order.pnl         = (exit_price - order.price) * order.qty * (1 if order.side == "BUY" else -1)
        order.status      = OrderStatus.CLOSED
        logger.info("Dhan exit placed for %s | exit_id=%s | pnl=%.2f",
                    order.symbol, order.exit_broker_order_id, order.pnl)
        return order

    def get_positions(self) -> list:
        if not self._dhan: return []
        try: return self._dhan.get_positions().get("data", []) or []
        except Exception: return []

    def get_funds(self) -> dict:
        if not self._dhan: return {}
        try:
            d = self._dhan.get_fund_limits().get("data", {})
            return {"available": d.get("availabelBalance", 0),
                    "used":      d.get("utilisedAmount",   0),
                    "total":     d.get("sodLimit",         0)}
        except Exception: return {}
