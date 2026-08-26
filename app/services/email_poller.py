import email
import imaplib
import logging
from datetime import date
from email.header import decode_header

from sqlalchemy.orm import Session

from app.config import settings
from app.models import MenuItem
from app.services.calorie_estimator import estimate_calories
from app.services.menu_llm_extractor import (
    MenuExtractionError,
    extract_menu_with_llm,
    extract_pdf_week_start,
)
from app.services.menu_parser import MenuParseError, parse_menu_email
from app.timezone import menu_target_week_start, now_local_naive

logger = logging.getLogger("email_poller")

# How many of the most recent inbox messages to scan for menu attachments.
MAX_MESSAGES_TO_SCAN = 20

# How many of the most recent menu emails to process on a normal (untargeted)
# refresh. The vendor sometimes sends next week's menu several days early,
# while this week's menu is still the one that should be active -- so a
# "grab the single newest email" approach can silently overwrite the active
# week with next week's menu. Processing the last two lets both the current
# and upcoming week stay populated, each under whichever week its own PDF
# footer says it's for (see extract_pdf_week_start).
RECENT_MENU_EMAILS_TO_PROCESS = 2

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


def fetch_recent_menu_sources(limit: int = RECENT_MENU_EMAILS_TO_PROCESS) -> list[tuple[str, bytes | str]]:
    """Connects via IMAP and scans the most recent inbox messages (newest
    first, optionally filtered by MENU_EMAIL_SENDER) for up to `limit` that
    have a PDF attachment or plain-text body that looks like a menu.
    Returns a list of ("pdf", pdf_bytes) / ("text", body) tuples, newest
    first."""
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

        sources: list[tuple[str, bytes | str]] = []
        for msg_id in reversed(ids):
            if len(sources) >= limit:
                break

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
                sources.append(("pdf", pdf_bytes))
                continue

            body = _extract_body(msg)
            if body.strip():
                sources.append(("text", body))

        if not sources:
            raise EmailPollError("No menu email with a PDF attachment or text body found")
        return sources
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _parse_menu_source(kind: str, payload: bytes | str) -> list:
    if kind == "pdf":
        try:
            return extract_menu_with_llm(payload)
        except MenuExtractionError as exc:
            raise EmailPollError(f"Menu extraction failed: {exc}") from exc

    try:
        parsed_items = parse_menu_email(payload)
    except MenuParseError as exc:
        raise EmailPollError(f"Menu text parsing failed: {exc}") from exc
    calories_by_name = estimate_calories(parsed_items)
    for item in parsed_items:
        item.calories_kcal = calories_by_name.get(item.item_name)
    return parsed_items


def _store_week(db: Session, week: date, parsed_items: list) -> None:
    for item in parsed_items:
        if item.category != SOUP_CATEGORY:
            item.price_czk = max(item.price_czk - NON_SOUP_DISCOUNT_CZK, 0)

    parsed_at = now_local_naive()
    db.query(MenuItem).filter(MenuItem.week_start == week).delete()
    db.add_all(
        MenuItem(
            week_start=week,
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
    logger.info("Menu refreshed: %d items for week of %s", len(parsed_items), week)


def refresh_menu(db: Session, target_week_start: date | None = None) -> int:
    """Fetches and bulk-replaces menu weeks from the inbox.

    With target_week_start given explicitly (e.g. an admin force-parsing a
    specific week from the admin panel), only the single newest menu email
    is used -- but it's still checked against its own "Týden ..." footer
    (when it's a PDF) before being stored. This used to trust the caller's
    target_week_start blindly, which meant force-parsing "next week" before
    next week's email had actually arrived would silently grab this week's
    email again and duplicate it under next week's date -- a real incident,
    not a hypothetical one. Now it raises instead of storing a mismatch. A
    text-fallback source has no footer to check (see extract_pdf_week_start,
    PDF-only) and a PDF whose footer can't be parsed falls back to trusting
    the caller, same as before -- both are pre-existing edge cases, not
    changed here.

    Otherwise (the normal scheduled/gap-check path), the last
    RECENT_MENU_EMAILS_TO_PROCESS menu emails are each parsed and stored
    under whichever week their own "Týden ..." footer states (falling back
    to menu_target_week_start() if that can't be read). The vendor
    sometimes sends next week's menu several days early, so trusting "the
    newest email = the current week" would silently blow away the still-
    active current week's menu; processing more than one keeps both
    current and upcoming weeks correct.

    Returns the total number of items stored across all weeks touched.
    PDF attachments are parsed by the LLM directly (see
    menu_llm_extractor), which estimates calories in the same call -- one
    LLM request instead of two. The plain-text fallback path has no LLM
    call to piggyback on, so it estimates calories separately."""
    if target_week_start is not None:
        kind, payload = fetch_recent_menu_sources(limit=1)[0]
        if kind == "pdf":
            actual_week = extract_pdf_week_start(payload)
            logger.info("force-parse target=%s actual_week_from_pdf=%s", target_week_start, actual_week)
            if actual_week is not None and actual_week != target_week_start:
                raise EmailPollError(
                    f"Newest menu email is for the week of {actual_week}, not "
                    f"{target_week_start} -- that week's menu hasn't arrived yet"
                )
        parsed_items = _parse_menu_source(kind, payload)
        _store_week(db, target_week_start, parsed_items)
        return len(parsed_items)

    sources = fetch_recent_menu_sources()
    total = 0
    for kind, payload in sources:
        parsed_items = _parse_menu_source(kind, payload)
        week = extract_pdf_week_start(payload) if kind == "pdf" else None
        if week is None:
            week = menu_target_week_start()
            logger.warning("Could not read week date from menu source, defaulting to %s", week)
        _store_week(db, week, parsed_items)
        total += len(parsed_items)
    return total
