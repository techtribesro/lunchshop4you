import email
import imaplib
import logging
from email.header import decode_header

from sqlalchemy.orm import Session

from app.config import settings
from app.models import MenuItem
from app.services.calorie_estimator import estimate_calories
from app.services.gemini_extractor import GeminiExtractionError, extract_menu_with_gemini
from app.services.menu_parser import MenuParseError, parse_menu_email
from app.timezone import now_local_naive, week_start

logger = logging.getLogger("email_poller")

# How many of the most recent inbox messages to scan for a menu attachment.
MAX_MESSAGES_TO_SCAN = 20

# The office has a standing 50 CZK discount off every dish except soup
# (soup is priced too low for the discount to make sense against it).
NON_SOUP_DISCOUNT_CZK = 50
SOUP_CATEGORY = "Polévka"


class EmailPollError(Exception):
    pass


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        for chunk, enc in parts
    )


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace") if payload else ""


def _find_pdf_attachment(msg: email.message.Message) -> bytes | None:
    for part in msg.walk():
        filename = _decode_header_value(part.get_filename())
        if filename.lower().endswith(".pdf"):
            return part.get_payload(decode=True)
    return None


def fetch_latest_menu_source() -> tuple[str, bytes | str]:
    """Connects via IMAP and scans the most recent inbox messages (newest
    first, optionally filtered by MENU_EMAIL_SENDER) for one with a PDF
    attachment or plain-text body that looks like a menu. Returns
    ("pdf", pdf_bytes) or ("text", body)."""
    if not settings.gmail_imap_user or not settings.gmail_imap_password:
        raise EmailPollError("Gmail IMAP credentials are not configured")

    conn = imaplib.IMAP4_SSL(settings.gmail_imap_host)
    try:
        conn.login(settings.gmail_imap_user, settings.gmail_imap_password)
        conn.select("INBOX")

        status, data = conn.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            raise EmailPollError("No messages found in inbox")

        ids = data[0].split()[-MAX_MESSAGES_TO_SCAN:]

        for msg_id in reversed(ids):
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            if settings.menu_email_sender:
                sender = msg.get("From") or ""
                if settings.menu_email_sender.lower() not in sender.lower():
                    continue

            pdf_bytes = _find_pdf_attachment(msg)
            if pdf_bytes:
                return "pdf", pdf_bytes

            body = _extract_body(msg)
            if body.strip():
                return "text", body

        raise EmailPollError("No menu email with a PDF attachment or text body found")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def refresh_menu(db: Session) -> int:
    """Fetches, parses, and bulk-replaces the current week's menu.
    Returns the number of items stored. PDF attachments are parsed by
    Gemini directly (see gemini_extractor), which estimates calories in the
    same call -- one Gemini request instead of two, which matters on the
    20-req/day free tier. The plain-text fallback path has no Gemini call
    to piggyback on, so it estimates calories separately. Prices are then
    adjusted for the office's standing non-soup discount (see
    NON_SOUP_DISCOUNT_CZK)."""
    kind, payload = fetch_latest_menu_source()

    if kind == "pdf":
        try:
            parsed_items = extract_menu_with_gemini(payload)
        except GeminiExtractionError as exc:
            raise EmailPollError(f"Gemini menu extraction failed: {exc}") from exc
    else:
        try:
            parsed_items = parse_menu_email(payload)
        except MenuParseError as exc:
            raise EmailPollError(f"Menu text parsing failed: {exc}") from exc
        calories_by_name = estimate_calories(parsed_items)
        for item in parsed_items:
            item.calories_kcal = calories_by_name.get(item.item_name)

    for item in parsed_items:
        if item.category != SOUP_CATEGORY:
            item.price_czk = max(item.price_czk - NON_SOUP_DISCOUNT_CZK, 0)

    current_week = week_start()
    parsed_at = now_local_naive()

    db.query(MenuItem).filter(MenuItem.week_start == current_week).delete()
    db.add_all(
        MenuItem(
            week_start=current_week,
            day=item.day,
            category=item.category,
            item_name=item.item_name,
            description=item.description,
            price_czk=item.price_czk,
            calories_kcal=item.calories_kcal,
            parsed_at=parsed_at,
        )
        for item in parsed_items
    )
    db.commit()

    logger.info("Menu refreshed: %d items for week of %s", len(parsed_items), current_week)
    return len(parsed_items)
