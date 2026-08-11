from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.db import get_db
from app.models import EarlyOrderingWindow, MenuItem, User
from app.schemas import AdminCreateUserRequest, AdminResetPasswordRequest, AdminSetCaloriesRequest, AdminUserOut
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.order_summary import OrderSummaryError, send_daily_order_summary
from app.services.sheets_sync import sync_all
from app.timezone import next_business_day, week_start

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/parse-menu")
def parse_menu(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    try:
        item_count = refresh_menu(db)
    except EmailPollError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    sync_all(db)
    return {"items_parsed": item_count}


@router.post("/clear-menu")
def clear_menu(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Wipes the current week's menu without re-fetching -- for clearing a
    bad parse before retrying, independent of the email pipeline."""
    deleted = db.query(MenuItem).filter(MenuItem.week_start == week_start()).delete()
    db.commit()
    sync_all(db)
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
    sync_all(db)
    return {"items_updated": updated}


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
