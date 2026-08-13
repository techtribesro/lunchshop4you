import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import TelegramSubscriber
from app.services.telegram_notify import TelegramError, send_telegram_message

logger = logging.getLogger("telegram_webhook")

router = APIRouter(prefix="/telegram", tags=["telegram"])

SUBSCRIBABLE_CHAT_TYPES = ("private", "group", "supergroup")

WELCOME_TEXT = (
    "Přihlášen/a k odběru denní objednávky obědů. Každý všední den ji sem "
    "pošleme spolu s e-mailem pro restauraci."
)


def _display_name_from_chat(chat: dict) -> str:
    if chat.get("type") in ("group", "supergroup"):
        return chat.get("title") or str(chat.get("id"))
    return (
        " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        or chat.get("username")
        or str(chat.get("id"))
    )


def _upsert_subscriber(db: Session, chat: dict) -> bool:
    """Returns True if this chat is newly subscribed."""
    chat_id = str(chat["id"])
    display_name = _display_name_from_chat(chat)

    existing = db.query(TelegramSubscriber).filter(TelegramSubscriber.chat_id == chat_id).first()
    if existing is None:
        db.add(TelegramSubscriber(chat_id=chat_id, display_name=display_name))
        db.commit()
        logger.info("New Telegram subscriber: %s (chat_id=%s)", display_name, chat_id)
        return True
    if existing.display_name != display_name:
        existing.display_name = display_name
        db.commit()
    return False


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Telegram calls this on every update involving the bot (see
    app.services.telegram_notify for how the webhook itself gets
    registered). Auto-subscribes whoever messages the bot directly, or
    whichever group/supergroup it's added to, to the daily order
    broadcast -- that's the whole point of the bot. Groups have privacy
    mode on by default (the bot only sees messages addressed to it, not
    every group message), so `my_chat_member` -- sent whenever the bot's
    own membership changes, regardless of privacy mode -- is the reliable
    signal for "the bot was just added to a group"."""
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret token")

    update = await request.json()

    my_chat_member = update.get("my_chat_member")
    if my_chat_member:
        chat = my_chat_member.get("chat") or {}
        new_status = (my_chat_member.get("new_chat_member") or {}).get("status")
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type") not in SUBSCRIBABLE_CHAT_TYPES:
            return {"ok": True}

        if new_status in ("member", "administrator", "creator"):
            if _upsert_subscriber(db, chat):
                try:
                    send_telegram_message(str(chat_id), WELCOME_TEXT)
                except TelegramError:
                    logger.exception("Failed to send welcome message to chat_id=%s", chat_id)
        elif new_status in ("left", "kicked"):
            db.query(TelegramSubscriber).filter(TelegramSubscriber.chat_id == str(chat_id)).delete()
            db.commit()
            logger.info("Telegram subscriber removed (bot left/kicked): chat_id=%s", chat_id)
        return {"ok": True}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") not in SUBSCRIBABLE_CHAT_TYPES:
        return {"ok": True}

    if _upsert_subscriber(db, chat):
        try:
            send_telegram_message(str(chat_id), WELCOME_TEXT)
        except TelegramError:
            logger.exception("Failed to send welcome message to chat_id=%s", chat_id)

    return {"ok": True}
