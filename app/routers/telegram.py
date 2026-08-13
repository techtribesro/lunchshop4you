import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import TelegramSubscriber
from app.services.telegram_notify import TelegramError, send_telegram_message

logger = logging.getLogger("telegram_webhook")

router = APIRouter(prefix="/telegram", tags=["telegram"])

WELCOME_TEXT = (
    "Přihlášen/a k odběru denní objednávky obědů. Každý všední den ji sem "
    "pošleme spolu s e-mailem pro restauraci."
)


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Telegram calls this on every message sent to the bot (see
    app.services.telegram_notify for how the webhook itself gets
    registered). Anyone who messages the bot is auto-subscribed to the
    daily order broadcast -- that's the whole point of the bot."""
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret token")

    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") != "private":
        return {"ok": True}

    display_name = (
        " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        or chat.get("username")
        or str(chat_id)
    )

    existing = db.query(TelegramSubscriber).filter(TelegramSubscriber.chat_id == str(chat_id)).first()
    is_new = existing is None
    if is_new:
        db.add(TelegramSubscriber(chat_id=str(chat_id), display_name=display_name))
        db.commit()
        logger.info("New Telegram subscriber: %s (chat_id=%s)", display_name, chat_id)
    elif existing.display_name != display_name:
        existing.display_name = display_name
        db.commit()

    if is_new:
        try:
            send_telegram_message(str(chat_id), WELCOME_TEXT)
        except TelegramError:
            logger.exception("Failed to send welcome message to new subscriber chat_id=%s", chat_id)

    return {"ok": True}
