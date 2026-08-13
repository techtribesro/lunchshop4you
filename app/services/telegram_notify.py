"""Sends the daily order summary to a Telegram chat via the Bot API, as an
additional best-effort notification alongside the restaurant email (see
order_summary.py) -- not a replacement. Callers should treat failures here
as non-fatal: the email is the channel that actually reaches the
restaurant, Telegram is a convenience notification on top of it.
"""

import html
import json
import logging
import ssl
import urllib.error
import urllib.request

import certifi

from app.config import settings
from app.models import Order
from app.services.order_formatting import format_date_cz, group_by_item

logger = logging.getLogger("telegram_notify")

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class TelegramError(Exception):
    pass


def _build_message(order_date, rows: list[tuple[str, Order]]) -> str:
    per_item = group_by_item(rows)
    lines = [
        f"<b>{html.escape(settings.order_summary_sender_name)} – Objednávka obědů</b>",
        html.escape(format_date_cz(order_date)),
        "",
    ]
    for item_name, data in per_item.items():
        lines.append(f"• {data['qty']}× {html.escape(item_name)} — {html.escape(', '.join(data['buyers']))}")
        if data["notes"]:
            lines.append(f"  ⚠️ {html.escape('; '.join(data['notes']))}")
    return "\n".join(lines)


def send_daily_order_telegram(order_date, rows: list[tuple[str, Order]]) -> None:
    """No-ops quietly if Telegram isn't configured -- this channel is
    optional, unlike the email which raises OrderSummaryError."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    body = json.dumps(
        {
            "chat_id": settings.telegram_chat_id,
            "text": _build_message(order_date, rows),
            "parse_mode": "HTML",
        }
    ).encode("utf-8")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15, context=_SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"Telegram request failed: {exc}") from exc

    if not payload.get("ok"):
        raise TelegramError(f"Telegram API error: {payload}")
