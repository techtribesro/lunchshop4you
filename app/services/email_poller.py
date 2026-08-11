import email
import imaplib
import io
import logging
from email.header import decode_header

import pdfplumber
from sqlalchemy.orm import Session

from app.config import settings
from app.models import MenuItem
from app.services.gemini_extractor import GeminiExtractionError, extract_menu_with_gemini
from app.services.menu_parser import parse_menu_email
from app.timezone import now_local_naive, week_start

logger = logging.getLogger("email_poller")

# Only used to score candidate text blocks pulled out of the menu PDF (see
# _extract_menu_text_from_pdf) -- independent of menu_parser's own diacritic
# handling.
DAY_HEADERS_ACCENTED = ["PONDĚLÍ", "ÚTERÝ", "STŘEDA", "ČTVRTEK", "PÁTEK"]

# How many of the most recent inbox messages to scan for a menu attachment.
MAX_MESSAGES_TO_SCAN = 20


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


def _extract_menu_text_from_pdf(pdf_bytes: bytes) -> str:
    """The vendor's PDF renders each day/category as separate text runs
    whose stream order doesn't match visual order, so a plain
    page.extract_text() call scrambles the menu. One of the table cells
    pdfplumber detects, however, preserves the real "Category: Item
    (allergens) Price" reading order -- so every extracted text block
    (whole-page text and every table cell) is scored by how much it looks
    like the menu, and the best-scoring one is used."""

    def score(text: str) -> int:
        return sum(text.count(day) for day in DAY_HEADERS_ACCENTED) + text.count("Kč")

    candidates: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            candidates.append(page.extract_text() or "")
            for table in page.extract_tables():
                for row in table:
                    for cell in row:
                        if cell:
                            candidates.append(cell)

    best = max(candidates, key=score, default="")
    if score(best) == 0:
        raise EmailPollError("Could not locate menu content in PDF attachment")
    return best


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
    Returns the number of items stored. PDFs are parsed via Gemini when
    configured (see gemini_extractor), falling back to the regex/heuristic
    parser on any failure since the LLM path is non-deterministic."""
    kind, payload = fetch_latest_menu_source()

    if kind == "pdf":
        try:
            parsed_items = extract_menu_with_gemini(payload)
        except GeminiExtractionError as exc:
            logger.warning("Gemini extraction failed (%s); falling back to regex parser", exc)
            parsed_items = parse_menu_email(_extract_menu_text_from_pdf(payload))
    else:
        parsed_items = parse_menu_email(payload)

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
            parsed_at=parsed_at,
        )
        for item in parsed_items
    )
    db.commit()

    logger.info("Menu refreshed: %d items for week of %s", len(parsed_items), current_week)
    return len(parsed_items)
