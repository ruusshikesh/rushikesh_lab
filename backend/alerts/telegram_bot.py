"""
Telegram bot — alerts + one-tap trade approval (semi-auto trading).

Satisfies the calls main.py already makes:
    telegram.kill_switch_alert(reason, daily_pnl)
    telegram.eod_report(orders, total_pnl, n_win, n_loss)

Adds the SEMI-AUTO approval flow you asked for:
    telegram.send_signal_alert(signal)     -> sends a trade with ✅ Approve / ❌ Skip
    telegram.handle_callback(update)        -> call from your webhook when a button is tapped

Config (config.py):  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Pending signals live in-memory keyed by a short id. When you tap ✅, handle_callback
looks up the signal and calls the provided order-placement callback (wired in main.py
to FyersClient.place_order). Tap ❌ and it's discarded.

To receive taps you need ONE of:
  • a webhook: set it once with set_webhook(<public_url>/telegram/webhook), OR
  • long-polling: run poll_updates() in a background task (simplest for a home box).
"""
from __future__ import annotations
import logging, time, threading
import requests
from config import settings

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self):
        self._pending = {}          # id -> signal dict
        self._seq = 0
        self._lock = threading.Lock()
        self._order_cb = None       # set via register_order_callback()
        self._poll_offset = 0

    # ── low-level send ─────────────────────────────────────────────────────────
    def _enabled(self) -> bool:
        return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)

    def _post(self, method: str, payload: dict) -> dict:
        if not self._enabled():
            logger.debug("Telegram disabled (no token/chat id) — skipping %s", method)
            return {}
        try:
            r = requests.post(API.format(token=settings.TELEGRAM_BOT_TOKEN, method=method),
                              json=payload, timeout=10)
            return r.json()
        except Exception as e:
            logger.warning("Telegram %s failed: %s", method, e)
            return {}

    def send(self, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    # ── the order callback (wired in main.py) ──────────────────────────────────
    def register_order_callback(self, cb):
        """cb(signal_dict) -> str (broker order id). Called when user taps Approve."""
        self._order_cb = cb

    # ── SEMI-AUTO: signal alert with Approve / Skip buttons ────────────────────
    def send_signal_alert(self, signal: dict):
        """
        signal: {symbol, side, qty, price, stop_loss, target1, score, reason, ...}
        Sends a formatted alert with two inline buttons.
        """
        with self._lock:
            self._seq += 1
            sid = str(self._seq)
            self._pending[sid] = dict(signal)

        s = signal
        txt = (
            f"📈 <b>TRADE SIGNAL</b>\n\n"
            f"<b>{s.get('symbol')}</b>  ·  {s.get('side','BUY')}\n"
            f"Qty: <b>{s.get('qty')}</b>  @  ₹{s.get('price')}\n"
            f"SL: ₹{s.get('stop_loss')}   Target: ₹{s.get('target1')}\n"
            f"Score: <b>{s.get('score','—')}</b>\n"
            f"{s.get('reason','')}\n\n"
            f"Approve to place this order."
        )
        kb = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ok:{sid}"},
            {"text": "❌ Skip",    "callback_data": f"no:{sid}"},
        ]]}
        self.send(txt, reply_markup=kb)

    # ── handle a button tap ────────────────────────────────────────────────────
    def handle_callback(self, update: dict):
        """Call this from your webhook (or poll loop) with a Telegram update object."""
        cq = update.get("callback_query")
        if not cq:
            return
        data = cq.get("data", "")
        cq_id = cq.get("id")
        msg = cq.get("message", {})
        mid = msg.get("message_id")

        action, _, sid = data.partition(":")
        with self._lock:
            signal = self._pending.pop(sid, None)

        # always answer the callback so the phone stops showing a spinner
        self._post("answerCallbackQuery", {"callback_query_id": cq_id})

        if signal is None:
            self._edit(mid, "⚠️ This signal expired or was already handled.")
            return

        if action == "no":
            self._edit(mid, f"❌ Skipped <b>{signal.get('symbol')}</b>.")
            return

        if action == "ok":
            if not self._order_cb:
                self._edit(mid, "⚠️ No order handler wired — cannot place order.")
                return
            try:
                oid = self._order_cb(signal)
                self._edit(mid, f"✅ Order placed for <b>{signal.get('symbol')}</b>\n"
                                f"Broker order id: <code>{oid}</code>")
            except Exception as e:
                logger.exception("Approve->order failed")
                self._edit(mid, f"🚫 Order FAILED for {signal.get('symbol')}: {e}")

    def _edit(self, message_id, text):
        self._post("editMessageText", {
            "chat_id": settings.TELEGRAM_CHAT_ID, "message_id": message_id,
            "text": text, "parse_mode": "HTML"})

    # ── webhook / polling helpers ──────────────────────────────────────────────
    def set_webhook(self, url: str):
        """One-time: point Telegram at <your_public_url>/telegram/webhook"""
        return self._post("setWebhook", {"url": url})

    def poll_updates(self):
        """Blocking long-poll loop (run in a background thread on a home box —
        avoids needing a public HTTPS webhook). Handles button taps forever."""
        if not self._enabled():
            logger.info("Telegram disabled — poll loop not started.")
            return
        logger.info("Telegram long-poll loop started.")
        while True:
            try:
                r = requests.get(
                    API.format(token=settings.TELEGRAM_BOT_TOKEN, method="getUpdates"),
                    params={"offset": self._poll_offset + 1, "timeout": 30},
                    timeout=35)
                for upd in r.json().get("result", []):
                    self._poll_offset = max(self._poll_offset, upd.get("update_id", 0))
                    self.handle_callback(upd)
            except Exception as e:
                logger.warning("poll_updates error: %s", e)
                time.sleep(3)

    # ── existing alerts main.py already calls ──────────────────────────────────
    def kill_switch_alert(self, reason: str, daily_pnl):
        self.send(f"🛑 <b>KILL SWITCH TRIGGERED</b>\nReason: {reason}\n"
                  f"Day P&L: ₹{daily_pnl}")

    def eod_report(self, orders, total_pnl, n_win, n_loss):
        n = len(orders) if hasattr(orders, "__len__") else 0
        self.send(f"📊 <b>End-of-Day Report</b>\nTrades: {n}\n"
                  f"Wins: {n_win}  Losses: {n_loss}\nTotal P&L: ₹{total_pnl}")

    # ── alerts the LiveScanner calls ───────────────────────────────────────────
    def order_alert(self, symbol, side, qty, price, sl, t1, t2, broker):
        """Plain notification that an entry order was placed (used in auto-trade
        mode). In APPROVE-FIRST mode the scanner should call send_signal_alert
        instead — this remains so the scanner never crashes if it calls it."""
        self.send(
            f"🟢 <b>ORDER PLACED</b> ({broker})\n"
            f"<b>{symbol}</b>  {side}  x{qty} @ ₹{price}\n"
            f"SL ₹{sl}  ·  T1 ₹{t1}  ·  T2 ₹{t2}"
        )

    def trade_closed_alert(self, symbol, pnl, exit_price, reason):
        emoji = "✅" if (pnl or 0) >= 0 else "🔴"
        self.send(
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"<b>{symbol}</b> exited @ ₹{exit_price}\n"
            f"P&L: ₹{round(pnl or 0, 2)}\nReason: {reason}"
        )

    def exit_failed_alert(self, symbol, reason, retry_count):
        self.send(
            f"⚠️ <b>EXIT FAILED</b>\n<b>{symbol}</b>\n"
            f"Reason: {reason}\nRetry #{retry_count} — check manually."
        )


# module-level singleton that main.py imports:  from alerts.telegram_bot import telegram
telegram = TelegramBot()
