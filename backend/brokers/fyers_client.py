"""
Fyers broker client — auth + order placement (FREE API).

Provides the interface main.py already expects:
    FyersClient.get_auth_url()        -> str   (OAuth login URL)
    FyersClient.exchange_code(code)   -> str   (auth code -> access_token)
    FyersClient.place_order(order)    -> str   (places order, returns order id)
    FyersClient.quote(symbol)         -> dict  (optional live quote)

Fyers Trading API is free — no subscription needed (only the separate desktop
"API Bridge" app costs money, which we do NOT use).

Requires:  pip install fyers-apiv3
Config (config.py): FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_ACCESS_TOKEN,
                    FYERS_REDIRECT_URI

Daily auth (manual paste flow):
     python -m brokers.fyers_client auth
  Prints the login URL (and opens the browser). You log in, Fyers redirects to
  your redirect_uri with ?auth_code=XXXX in the address bar. Copy that auth_code,
  paste it into the terminal, and it prints the access token to put in .env.
"""
from __future__ import annotations
import logging
from config import settings

logger = logging.getLogger(__name__)


def _to_fyers(symbol: str) -> str:
    """NSE symbol -> Fyers format (e.g. 'RELIANCE' -> 'NSE:RELIANCE-EQ').
    Mirrors the helper in data/fetcher.py so orders and data agree."""
    s = symbol.upper().replace("NSE:", "").replace("-EQ", "")
    if s in ("NIFTY", "NIFTY50"): return "NSE:NIFTY50-INDEX"
    if s == "BANKNIFTY":          return "NSE:NIFTYBANK-INDEX"
    return f"NSE:{s}-EQ"


def _session():
    """Build a Fyers session model for the login/token exchange."""
    try:
        from fyers_apiv3 import fyersModel
    except ImportError as e:
        raise RuntimeError("fyers-apiv3 not installed. Run: pip install fyers-apiv3") from e
    return fyersModel.SessionModel(
        client_id=settings.FYERS_APP_ID,
        secret_key=settings.FYERS_SECRET_KEY,
        redirect_uri=settings.FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )


def _fyers():
    """Authenticated Fyers model for placing orders / quotes."""
    try:
        from fyers_apiv3 import fyersModel
    except ImportError as e:
        raise RuntimeError("fyers-apiv3 not installed. Run: pip install fyers-apiv3") from e
    if not settings.FYERS_ACCESS_TOKEN:
        raise RuntimeError(
            "FYERS_ACCESS_TOKEN is empty — complete the daily login "
            "(/auth/login?broker=fyers) and set the token first."
        )
    return fyersModel.FyersModel(
        client_id=settings.FYERS_APP_ID,
        token=settings.FYERS_ACCESS_TOKEN,
        is_async=False,
    )


class FyersClient:

    # ── auth ──────────────────────────────────────────────────────────────────
    @staticmethod
    def get_auth_url() -> str:
        return _session().generate_authcode()

    @staticmethod
    def exchange_code(auth_code: str) -> str:
        if not auth_code:
            raise ValueError("auth_code is empty")
        sess = _session()
        sess.set_token(auth_code)
        resp = sess.generate_token()
        token = resp.get("access_token")
        if not token:
            raise RuntimeError(f"Fyers token exchange failed: {resp}")
        logger.info("Fyers session established; access_token acquired.")
        return token

    # ── order placement ───────────────────────────────────────────────────────
    @staticmethod
    def place_order(order) -> str:
        """
        Place an order on Fyers. `order` is schemas.Order. Returns broker order id.

        Fyers order dict fields:
          side:  1 = BUY, -1 = SELL
          type:  1 = LIMIT, 2 = MARKET
          productType: "INTRADAY" | "CNC"
        """
        fy = _fyers()

        side = 1 if str(order.side).upper() == "BUY" else -1
        otype = 2 if str(order.order_type).upper() == "MARKET" else 1

        product = "INTRADAY"
        try:
            tt = str(getattr(order, "trade_type", "intraday")).lower()
            product = "CNC" if ("deliver" in tt or "cnc" in tt) else "INTRADAY"
        except Exception:
            pass

        data = {
            "symbol": _to_fyers(order.symbol),
            "qty": int(order.qty),
            "type": otype,
            "side": side,
            "productType": product,
            "limitPrice": float(order.price) if otype == 1 else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        logger.info("Fyers place_order: %s", data)
        resp = fy.place_order(data=data)
        # success looks like {"s":"ok","id":"...","message":...}
        if isinstance(resp, dict) and resp.get("s") == "ok" and resp.get("id"):
            oid = str(resp["id"])
            logger.info("Fyers order placed, id=%s", oid)
            return oid
        raise RuntimeError(f"Fyers order failed: {resp}")

    # ── optional: live quote ──────────────────────────────────────────────────
    @staticmethod
    def quote(symbol: str) -> dict:
        fy = _fyers()
        resp = fy.quotes({"symbols": _to_fyers(symbol)})
        return resp


# ── CLI: python -m brokers.fyers_client auth ──────────────────────────────────
# Manual paste flow: prints the login URL (and opens the browser). Log in, then
# copy the auth_code from the redirected address bar and paste it below. Prints
# the access token to put in .env as FYERS_ACCESS_TOKEN. No server, no port needed.
if __name__ == "__main__":
    import sys
    import webbrowser

    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    if cmd != "auth":
        print(f"Unknown command: {cmd}. Use: python -m brokers.fyers_client auth")
        sys.exit(1)

    url = FyersClient.get_auth_url()
    print("\n" + "=" * 70)
    print("STEP 1 — a browser will open. Log in to Fyers.")
    print("If it doesn't open, paste this URL manually:")
    print(url)
    print("=" * 70)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("\nSTEP 2 — after login the browser redirects to a URL like:")
    print("  https://127.0.0.1:8000/fyers/callback?auth_code=XXXXXX&state=None")
    print("(The page may show a connection error — that's fine, the code is in")
    print(" the address bar.) Copy the auth_code value and paste it below.\n")

    auth_code = input("Paste auth_code here: ").strip()
    if not auth_code:
        print("No auth_code entered. Aborting.")
        sys.exit(1)

    print("\nExchanging for access token...")
    token = FyersClient.exchange_code(auth_code)

    print("\n" + "=" * 70)
    print("SUCCESS — your access token:")
    print(token)
    print("=" * 70)
    print("\nPut this line in your .env (replace the old one):")
    print(f"FYERS_ACCESS_TOKEN={token}")
    print("\nThen restart the backend:  uvicorn main:app --port 8000\n")
