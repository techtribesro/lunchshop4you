"""Sends the day's lunch order to the restaurant by email, on demand (admin
panel button, no automatic schedule). Builds an HTML table rather than an
.xlsx attachment -- readable directly in the inbox, no dependency on a
spreadsheet library.
"""

import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order, User
from app.services.order_formatting import format_date_cz, group_by_item
from app.services.telegram_notify import TelegramError, send_daily_order_telegram
from app.timezone import today_local

logger = logging.getLogger("order_summary")

# Static rather than Gemini-generated: this email sends automatically every
# weekday, and the free-tier Gemini quota (20 requests/day, shared with menu
# parsing and calorie estimation) is too tight to spend one call a day on a
# greeting. Not fully correct Czech vocative case for an arbitrary name, but
# ORDER_SUMMARY_RECIPIENT_NAME is fixed per deployment, so it only needs to
# read right once.
EMAIL_COPY = {
    "greeting": f"Dobrý den, {settings.order_summary_recipient_name},",
    "intro": "posíláme naši dnešní objednávku obědů, viz tabulka níže:",
    "thanks": "Děkujeme,",
}


class OrderSummaryError(Exception):
    pass


def _fetch_order_rows(db: Session, order_date: date) -> list[tuple[str, Order]]:
    orders = (
        db.query(Order, User.username)
        .join(User, User.id == Order.user_id)
        .filter(Order.order_date == order_date)
        .order_by(User.username, Order.item_name)
        .all()
    )
    return [(username, order) for order, username in orders]


def _build_html(order_date: date, rows: list[tuple[str, Order]], copy: dict[str, str]) -> str:
    per_item = group_by_item(rows)

    def notes_cell(notes: list[str]) -> str:
        if not notes:
            return '<span style="color:#5b6156;">&mdash;</span>'
        return (
            '<span style="display:inline-block;border:1px solid #c0392b;border-radius:4px;'
            'padding:3px 7px;color:#c0392b;">' + "; ".join(notes) + "</span>"
        )

    summary_rows = "".join(
        f"""<tr>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{item_name}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;text-align:center;font-weight:600;">{data['qty']}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{", ".join(data["buyers"])}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #e3e1d4;">{notes_cell(data["notes"])}</td>
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
            <th style="padding:6px 10px;text-align:left;">Jídlo</th>
            <th style="padding:6px 10px;text-align:center;">Počet</th>
            <th style="padding:6px 10px;text-align:left;">Kdo</th>
            <th style="padding:6px 10px;text-align:left;">Poznámky</th>
          </tr>
        </thead>
        <tbody>{summary_rows}</tbody>
      </table>

      <p>{copy["thanks"]}<br>{settings.order_summary_sender_name}</p>
    </div>
    """


def _build_text(order_date: date, rows: list[tuple[str, Order]], copy: dict[str, str]) -> str:
    per_item = group_by_item(rows)

    lines = [copy["greeting"], "", copy["intro"], ""]
    for item_name, data in per_item.items():
        notes = f" [{'; '.join(data['notes'])}]" if data["notes"] else ""
        lines.append(f"- {data['qty']}x {item_name} ({', '.join(data['buyers'])}){notes}")
    lines += ["", copy["thanks"], settings.order_summary_sender_name]
    return "\n".join(lines)


def send_daily_order_summary(db: Session, order_date: date | None = None) -> int:
    """Sends today's order via whichever channels are enabled: email to
    settings.order_summary_recipient_email (unless disabled via
    ORDER_SUMMARY_EMAIL_ENABLED=false) and/or Telegram. The two are
    independent -- Telegram still fires even if email is disabled or
    fails, since it's meant as a fallback delivery path, not something
    conditional on the email succeeding first. Returns the number of
    order lines included (0 if there were no orders)."""
    order_date = order_date or today_local()

    rows = _fetch_order_rows(db, order_date)
    if not rows:
        logger.info("No orders for %s; skipping summary", order_date)
        return 0

    email_error: OrderSummaryError | None = None

    if not settings.order_summary_email_enabled:
        logger.info("Order-summary email disabled (ORDER_SUMMARY_EMAIL_ENABLED=false); skipping")
    elif not settings.order_summary_recipient_email:
        email_error = OrderSummaryError("ORDER_SUMMARY_RECIPIENT_EMAIL is not configured")
    elif not settings.gmail_imap_user or not settings.gmail_imap_password:
        email_error = OrderSummaryError("Gmail credentials are not configured")
    else:
        message = MIMEMultipart("alternative")
        message["Subject"] = (
            f"{settings.order_summary_sender_name} – Objednávka obědů – {format_date_cz(order_date)}"
        )
        message["From"] = f"{settings.order_summary_sender_name} <{settings.gmail_imap_user}>"
        message["To"] = settings.order_summary_recipient_email
        message.attach(MIMEText(_build_text(order_date, rows, EMAIL_COPY), "plain", "utf-8"))
        message.attach(MIMEText(_build_html(order_date, rows, EMAIL_COPY), "html", "utf-8"))

        try:
            with smtplib.SMTP(settings.gmail_smtp_host, settings.gmail_smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(settings.gmail_imap_user, settings.gmail_imap_password)
                smtp.sendmail(settings.gmail_imap_user, [settings.order_summary_recipient_email], message.as_string())
            logger.info(
                "Order summary sent for %s: %d line(s) to %s", order_date, len(rows), settings.order_summary_recipient_email
            )
        except smtplib.SMTPException as exc:
            email_error = OrderSummaryError(f"Failed to send order summary email: {exc}")

    try:
        send_daily_order_telegram(db, order_date, rows)
    except TelegramError:
        logger.exception("Telegram notification failed; continuing")

    if email_error is not None:
        raise email_error

    return len(rows)


def send_telegram_only(db: Session, order_date: date | None = None) -> int:
    """Fires just the Telegram broadcast, independent of the restaurant
    email -- for testing the Telegram side on its own, or re-notifying
    subscribers without re-sending the email. Unlike the best-effort
    Telegram step inside send_daily_order_summary(), this raises
    TelegramError on failure since it's the caller's whole point."""
    order_date = order_date or today_local()

    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")

    rows = _fetch_order_rows(db, order_date)
    if not rows:
        logger.info("No orders for %s; skipping Telegram-only send", order_date)
        return 0

    send_daily_order_telegram(db, order_date, rows)
    return len(rows)
