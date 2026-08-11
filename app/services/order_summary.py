"""Sends the day's lunch order to the restaurant by email, shortly after the
11:30 cutoff (see app.services.scheduler). Builds an HTML table rather than
an .xlsx attachment -- readable directly in the inbox, no dependency on a
spreadsheet library.
"""

import logging
import smtplib
from collections import defaultdict
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order, User
from app.services.gemini_client import GeminiError, generate_json
from app.timezone import today_local

logger = logging.getLogger("order_summary")

DAY_NAMES_CZ = {
    0: "pondělí",
    1: "úterý",
    2: "středa",
    3: "čtvrtek",
    4: "pátek",
    5: "sobota",
    6: "neděle",
}

COPY_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "greeting": {"type": "STRING"},
        "intro": {"type": "STRING"},
        "thanks": {"type": "STRING"},
    },
    "required": ["greeting", "intro", "thanks"],
}

# Used only if Gemini is unconfigured or the call fails -- keeps the email
# sendable, at the cost of a plain (non-vocative) greeting.
FALLBACK_COPY = {
    "greeting": f"Dobrý den, {settings.order_summary_recipient_name},",
    "intro": "posíláme naši dnešní objednávku obědů, viz tabulka níže:",
    "thanks": "Děkujeme,",
}


class OrderSummaryError(Exception):
    pass


def _format_date_cz(d: date) -> str:
    return f"{DAY_NAMES_CZ[d.weekday()]} {d.day}. {d.month}. {d.year}"


def _generate_copy(order_date: date) -> dict[str, str]:
    """Has Gemini write the Czech greeting/intro/thanks lines -- correct
    vocative case and natural phrasing aren't something to hand-roll in
    Python. The order data itself (table rows) never goes through the LLM;
    only this wrapper prose does, and a static fallback covers the case
    where Gemini is unavailable."""
    prompt = f"""Write copy for a short daily work email in Czech, sent by an office
colleague ordering lunch to a restaurant contact.

Context:
- Recipient's first name: {settings.order_summary_recipient_name}
- Today's date (already Czech-formatted): {_format_date_cz(order_date)}
- Right after your "intro" line, the email inserts a table with today's lunch
  order, then your "thanks" line followed by the sender's name (appended
  separately -- do not include any name in "thanks").

Return JSON with:
- "greeting": a short opening line addressing the recipient by name in the
  correct Czech vocative case (e.g. "Dobrý den, Honzo,")
- "intro": one short sentence saying today's lunch order follows below,
  naturally referencing the date
- "thanks": a brief closing thanks phrase on its own, e.g. "Děkujeme," --
  no name

Tone: friendly, professional, concise -- like real Czech office
correspondence between colleagues who know each other, not stiff or
robotic."""

    try:
        result = generate_json([{"text": prompt}], COPY_RESPONSE_SCHEMA)
        if not isinstance(result, dict) or not all(k in result for k in ("greeting", "intro", "thanks")):
            raise GeminiError(f"Unexpected copy shape: {result!r}")
        return {k: str(result[k]) for k in ("greeting", "intro", "thanks")}
    except GeminiError as exc:
        logger.warning("Gemini copy generation failed (%s); using fallback text", exc)
        return FALLBACK_COPY


def _build_html(order_date: date, rows: list[tuple[str, Order]], copy: dict[str, str]) -> str:
    per_item: dict[str, dict[str, int | list[str]]] = defaultdict(lambda: {"qty": 0, "notes": []})
    for username, order in rows:
        entry = per_item[order.item_name]
        entry["qty"] += order.quantity
        if order.note:
            entry["notes"].append(f"{order.note} ({username})")

    person_rows = "".join(
        f"""<tr>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{username}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{order.item_name}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;text-align:center;">{order.quantity}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;color:#5b6156;">{order.note or "&mdash;"}</td>
            </tr>"""
        for username, order in rows
    )

    summary_rows = "".join(
        f"""<tr>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{item_name}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;text-align:center;font-weight:600;">{data['qty']}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;color:#5b6156;">{"; ".join(data["notes"]) or "&mdash;"}</td>
            </tr>"""
        for item_name, data in per_item.items()
    )

    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#22261f;max-width:640px;">
      <p>{copy["greeting"]}</p>
      <p>{copy["intro"]}</p>

      <table style="border-collapse:collapse;width:100%;margin-bottom:20px;">
        <thead>
          <tr style="background:#e3ead9;">
            <th style="padding:6px 10px;text-align:left;">Jméno</th>
            <th style="padding:6px 10px;text-align:left;">Jídlo</th>
            <th style="padding:6px 10px;text-align:center;">Počet</th>
            <th style="padding:6px 10px;text-align:left;">Poznámka</th>
          </tr>
        </thead>
        <tbody>{person_rows}</tbody>
      </table>

      <p style="font-weight:600;margin-bottom:6px;">Souhrn podle jídla</p>
      <table style="border-collapse:collapse;width:100%;margin-bottom:20px;">
        <thead>
          <tr style="background:#e3ead9;">
            <th style="padding:6px 10px;text-align:left;">Jídlo</th>
            <th style="padding:6px 10px;text-align:center;">Celkem ks</th>
            <th style="padding:6px 10px;text-align:left;">Poznámky</th>
          </tr>
        </thead>
        <tbody>{summary_rows}</tbody>
      </table>

      <p>{copy["thanks"]}<br>{settings.order_summary_sender_name}</p>
    </div>
    """


def _build_text(order_date: date, rows: list[tuple[str, Order]], copy: dict[str, str]) -> str:
    lines = [copy["greeting"], "", copy["intro"], ""]
    for username, order in rows:
        note = f" [{order.note}]" if order.note else ""
        lines.append(f"- {username}: {order.quantity}x {order.item_name}{note}")
    lines += ["", copy["thanks"], settings.order_summary_sender_name]
    return "\n".join(lines)


def send_daily_order_summary(db: Session, order_date: date | None = None) -> int:
    """Emails today's order to settings.order_summary_recipient_email.
    Returns the number of order lines included (0 if nothing was sent,
    either because there were no orders or the recipient isn't configured)."""
    order_date = order_date or today_local()

    if not settings.order_summary_recipient_email:
        raise OrderSummaryError("ORDER_SUMMARY_RECIPIENT_EMAIL is not configured")
    if not settings.gmail_imap_user or not settings.gmail_imap_password:
        raise OrderSummaryError("Gmail credentials are not configured")

    orders = (
        db.query(Order, User.username)
        .join(User, User.id == Order.user_id)
        .filter(Order.order_date == order_date)
        .order_by(User.username, Order.item_name)
        .all()
    )
    rows = [(username, order) for order, username in orders]

    if not rows:
        logger.info("No orders for %s; skipping summary email", order_date)
        return 0

    copy = _generate_copy(order_date)

    message = MIMEMultipart("alternative")
    message["Subject"] = f"Objednávka obědů – {_format_date_cz(order_date)}"
    message["From"] = f"{settings.order_summary_sender_name} <{settings.gmail_imap_user}>"
    message["To"] = settings.order_summary_recipient_email
    message.attach(MIMEText(_build_text(order_date, rows, copy), "plain", "utf-8"))
    message.attach(MIMEText(_build_html(order_date, rows, copy), "html", "utf-8"))

    try:
        with smtplib.SMTP(settings.gmail_smtp_host, settings.gmail_smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.gmail_imap_user, settings.gmail_imap_password)
            smtp.sendmail(settings.gmail_imap_user, [settings.order_summary_recipient_email], message.as_string())
    except smtplib.SMTPException as exc:
        raise OrderSummaryError(f"Failed to send order summary email: {exc}") from exc

    logger.info("Order summary sent for %s: %d line(s) to %s", order_date, len(rows), settings.order_summary_recipient_email)
    return len(rows)
