import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.db import get_db
from app.models import EarlyOrderingWindow, MenuItem, Order, TelegramSubscriber, User
from app.schemas import (
    AdminAddTelegramSubscriberRequest,
    AdminAssignOrderRequest,
    AdminCreateUserRequest,
    AdminResetPasswordRequest,
    AdminSetCaloriesRequest,
    AdminSetPricesRequest,
    AdminUserOut,
    OrderLineOut,
    TelegramSubscriberOut,
)
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.order_summary import OrderSummaryError, send_daily_order_summary, send_telegram_only
from app.services.sheets_sync import sync_all
from app.services.telegram_notify import TelegramError
from app.timezone import next_business_day, week_start

logger = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _sync_all_best_effort(db: Session) -> None:
    """Sheets sync is a reporting mirror, not the source of truth -- an
    error here (API failure, bad credentials, etc.) must never be allowed
    to make an otherwise-successful DB write look like it failed to the
    caller. (Doesn't protect against the process being OOM-killed mid-sync
    -- that still drops the response -- but the DB write underneath has
    already been committed by that point regardless.)"""
    try:
        sync_all(db)
    except Exception:
        logger.exception("Sheets sync failed after a successful admin write; continuing")


@router.post("/parse-menu")
def parse_menu(
    for_next_week: bool = False,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """for_next_week=True stores the parsed menu under next week's
    Monday instead of the current week -- for when next week's menu
    email has already arrived early (mid-week) and you don't want to
    wait for the normal Sunday-evening poll."""
    target = week_start() + timedelta(days=7) if for_next_week else None
    try:
        item_count = refresh_menu(db, target_week_start=target)
    except EmailPollError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    _sync_all_best_effort(db)
    return {"items_parsed": item_count, "week_start": (target or week_start()).isoformat()}


@router.post("/clear-menu")
def clear_menu(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Wipes the current week's menu without re-fetching -- for clearing a
    bad parse before retrying, independent of the email pipeline."""
    deleted = db.query(MenuItem).filter(MenuItem.week_start == week_start()).delete()
    db.commit()
    _sync_all_best_effort(db)
    return {"items_deleted": deleted}


@router.post("/menu/calories")
def set_menu_calories(
    payload: AdminSetCaloriesRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Manual kcal backfill for the current week's menu, bypassing Gemini --
    for filling in estimates without spending free-tier quota (e.g. when the
    menu was parsed before the kcal-in-extraction feature existed)."""
    current_week = week_start()
    updated = 0
    for entry in payload.items:
        result = (
            db.query(MenuItem)
            .filter(
                MenuItem.week_start == current_week,
                MenuItem.day == entry.day,
                MenuItem.item_name == entry.item_name,
            )
            .update({"calories_kcal": entry.calories_kcal})
        )
        updated += result
    db.commit()
    _sync_all_best_effort(db)
    return {"items_updated": updated}


@router.post("/menu/prices")
def set_menu_prices(
    payload: AdminSetPricesRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Manual price backfill for the current week's menu -- sets absolute
    prices (idempotent: calling it twice with the same payload is a no-op),
    unlike a relative adjustment which would compound on every retry."""
    current_week = week_start()
    updated = 0
    for entry in payload.items:
        result = (
            db.query(MenuItem)
            .filter(
                MenuItem.week_start == current_week,
                MenuItem.day == entry.day,
                MenuItem.item_name == entry.item_name,
            )
            .update({"price_czk": entry.price_czk})
        )
        updated += result
    db.commit()
    _sync_all_best_effort(db)
    return {"items_updated": updated}


@router.get("/orders", response_model=list[OrderLineOut])
def get_assigned_order(
    username: str,
    order_date: date,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    target_user = db.query(User).filter(User.username == username).first()
    if target_user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{username}' not found")
    return (
        db.query(Order)
        .filter(Order.user_id == target_user.id, Order.order_date == order_date)
        .order_by(Order.item_name)
        .all()
    )


@router.post("/orders", response_model=list[OrderLineOut])
def assign_order(
    payload: AdminAssignOrderRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Lets an admin set a user's order for a given date directly -- bypasses
    the normal cutoff/early-ordering rules, since the whole point is to fill
    in orders on someone's behalf (forgot to order, out of office, etc.)."""
    target_user = db.query(User).filter(User.username == payload.username).first()
    if target_user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{payload.username}' not found")

    if payload.order_date.weekday() >= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No ordering on weekends")

    day_name = DAY_NAMES[payload.order_date.weekday()]
    target_week_start = week_start(payload.order_date)

    menu_by_name = {
        item.item_name: item
        for item in db.query(MenuItem)
        .filter(MenuItem.week_start == target_week_start, MenuItem.day == day_name)
        .all()
    }

    for line in payload.items:
        if line.item_name not in menu_by_name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"'{line.item_name}' is not on the menu for {payload.order_date}"
            )
        if line.quantity < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be at least 1")

    db.query(Order).filter(Order.user_id == target_user.id, Order.order_date == payload.order_date).delete()

    new_orders = [
        Order(
            user_id=target_user.id,
            order_date=payload.order_date,
            week_start=target_week_start,
            item_name=line.item_name,
            quantity=line.quantity,
            unit_price_czk=menu_by_name[line.item_name].price_czk,
            note=line.note.strip()[:255],
        )
        for line in payload.items
    ]
    db.add_all(new_orders)
    db.commit()

    return db.query(Order).filter(Order.user_id == target_user.id, Order.order_date == payload.order_date).all()


@router.delete("/orders/by-date")
def clear_orders_for_date(
    order_date: date,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Wipes every user's order for a given date -- for clearing out test/junk
    data, independent of the normal per-user order flow."""
    deleted = db.query(Order).filter(Order.order_date == order_date).delete()
    db.commit()
    return {"orders_deleted": deleted}


@router.post("/send-order-summary")
def send_order_summary(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    try:
        line_count = send_daily_order_summary(db)
    except OrderSummaryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {"order_lines_sent": line_count}


@router.post("/send-order-telegram")
def send_order_telegram(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Fires just the Telegram broadcast, independent of the restaurant
    email -- for testing, or re-notifying subscribers without re-sending
    the email."""
    try:
        line_count = send_telegram_only(db)
    except TelegramError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {"order_lines_sent": line_count}


@router.get("/early-ordering")
def get_early_ordering_status(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    target = next_business_day()
    window = db.query(EarlyOrderingWindow).filter(EarlyOrderingWindow.order_date == target).first()
    return {"date": target.isoformat(), "open": window is not None}


@router.post("/early-ordering/open")
def open_early_ordering(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    target = next_business_day()
    if week_start(target) != week_start():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Next business day falls in a week whose menu isn't loaded yet -- can't open ordering for it.",
        )
    if not db.query(EarlyOrderingWindow).filter(EarlyOrderingWindow.order_date == target).first():
        db.add(EarlyOrderingWindow(order_date=target, opened_by=admin.username))
        db.commit()
    return {"date": target.isoformat(), "open": True}


@router.post("/early-ordering/close")
def close_early_ordering(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    target = next_business_day()
    db.query(EarlyOrderingWindow).filter(EarlyOrderingWindow.order_date == target).delete()
    db.commit()
    return {"date": target.isoformat(), "open": False}


# Stale windows (the date has already passed -- normal cutoff rules take
# over once a date becomes "today") are left in place rather than cleaned
# up; they're harmless and _check_ordering_allowed only ever looks up the
# exact target date, so old rows are simply never matched again.


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return db.query(User).order_by(User.username).all()


@router.post("/users", response_model=AdminUserOut)
def create_user(
    payload: AdminCreateUserRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"User '{payload.username}' already exists")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    return user


@router.post("/users/{username}/reset-password")
def reset_user_password(
    username: str,
    payload: AdminResetPasswordRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{username}' not found")

    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@router.post("/users/{username}/toggle-admin", response_model=AdminUserOut)
def toggle_admin(
    username: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{username}' not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot change your own admin status")

    user.is_admin = not user.is_admin
    db.commit()
    return user


@router.get("/telegram-subscribers", response_model=list[TelegramSubscriberOut])
def list_telegram_subscribers(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return db.query(TelegramSubscriber).order_by(TelegramSubscriber.subscribed_at).all()


@router.post("/telegram-subscribers", response_model=TelegramSubscriberOut)
def add_telegram_subscriber(
    payload: AdminAddTelegramSubscriberRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """For adding a subscriber by chat_id directly -- e.g. one already known
    from an earlier /getUpdates lookup. The normal path is the webhook
    auto-subscribing whoever messages the bot; this is the manual escape
    hatch for that."""
    if db.query(TelegramSubscriber).filter(TelegramSubscriber.chat_id == payload.chat_id).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"chat_id '{payload.chat_id}' is already subscribed")

    subscriber = TelegramSubscriber(chat_id=payload.chat_id, display_name=payload.display_name or payload.chat_id)
    db.add(subscriber)
    db.commit()
    return subscriber


@router.delete("/telegram-subscribers/{chat_id}")
def remove_telegram_subscriber(
    chat_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    deleted = db.query(TelegramSubscriber).filter(TelegramSubscriber.chat_id == chat_id).delete()
    db.commit()
    return {"removed": bool(deleted)}
