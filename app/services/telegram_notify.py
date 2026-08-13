"""Sends the daily order summary to every subscribed Telegram chat via the
Bot API, as an additional best-effort notification alongside the
restaurant email (see order_summary.py) -- not a replacement. Anyone who
messages the bot is auto-subscribed (see app.routers.telegram), so if the
restaurant email doesn't land, anyone on the broadcast list can forward
the message manually. Failures here are non-fatal: the email is the
channel that actually reaches the restaurant.
"""

import html
import json
import logging
import ssl
import urllib.error
import urllib.request
from collections import defaultdict

import certifi
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order, TelegramSubscriber
from app.services.order_formatting import format_date_cz

logger = logging.getLogger("telegram_notify")

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class TelegramError(Exception):
    pass


def _build_message(order_date, rows: list[tuple[str, Order]]) -> str:
    per_user: dict[str, list[Order]] = defaultdict(list)
    for username, order in rows:
        per_user[username].append(order)

    lines = [
        f"<b>{html.escape(settings.order_summary_sender_name)} – Objednávka obědů</b>",
        html.escape(format_date_cz(order_date)),
        "",
    ]
    for username, orders in per_user.items():
        lines.append(f"<b>{html.escape(username)}</b>")
        for order in orders:
            line = f"• {html.escape(order.item_name)} ×{order.quantity}"
            if order.note:
                line += f" ⚠️ {html.escape(order.note)}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip()


def send_telegram_message(chat_id: str, text: str) -> None:
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")

    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15, context=_SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"Telegram request failed: {exc}") from exc

    if not payload.get("ok"):
        raise TelegramError(f"Telegram API error: {payload}")


def send_daily_order_telegram(db: Session, order_date, rows: list[tuple[str, Order]]) -> None:
    """No-ops quietly if Telegram isn't configured or nobody has
    subscribed yet -- this channel is optional, unlike the email which
    raises OrderSummaryError."""
    if not settings.telegram_bot_token:
        return
    subscribers = db.query(TelegramSubscriber).all()
    if not subscribers:
        return

    text = _build_message(order_date, rows)
    for sub in subscribers:
        try:
            send_telegram_message(sub.chat_id, text)
        except TelegramError:
            logger.exception("Telegram send failed for %s (chat_id=%s)", sub.display_name, sub.chat_id)
