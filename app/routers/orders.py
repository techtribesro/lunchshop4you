from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import MenuItem, Order, User
from app.schemas import OrderLineOut, OrderSubmitRequest
from app.services.sheets_sync import sync_all
from app.timezone import today_local, week_start

router = APIRouter(prefix="/orders", tags=["orders"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _check_ordering_allowed(target_date) -> None:
    """No day-locking, no cutoff -- any weekday in a loaded menu week can be
    ordered/edited freely. Only past dates and weekends are blocked."""
    today = today_local()

    if target_date < today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot order for a past date")

    if target_date.weekday() >= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No ordering on weekends")


@router.post("", response_model=list[OrderLineOut])
def submit_order(
    payload: OrderSubmitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Submit (replacing) one day's order lines.

    AUTHORIZATION, changed 2026-09-11 by operator decision: `on_behalf_of`
    lets ANY logged-in user submit for another *existing registered* user.
    This used to be admin-only (via /admin/orders). Deliberate and reviewed.

    ACCEPTED RISK: the delete-then-reinsert below replaces every row the
    target user has for that date, so an on-behalf submit silently overwrites
    a colleague's existing order. The operator was shown this and chose to
    accept it; do not add merge/confirm/audit machinery to guard it.
    """
    order_user = user
    if payload.on_behalf_of and payload.on_behalf_of != user.username:
        order_user = db.query(User).filter(User.username == payload.on_behalf_of).first()
        if order_user is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"User '{payload.on_behalf_of}' not found"
            )

    target_date = payload.order_date or today_local()
    _check_ordering_allowed(target_date)

    day_name = DAY_NAMES[target_date.weekday()]
    target_week_start = week_start(target_date)

    menu_by_name = {
        item.item_name: item
        for item in db.query(MenuItem)
        .filter(MenuItem.week_start == target_week_start, MenuItem.day == day_name)
        .all()
    }

    for line in payload.items:
        if line.item_name not in menu_by_name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"'{line.item_name}' is not on the menu for {target_date}"
            )
        if line.quantity < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be at least 1")

    db.query(Order).filter(
        Order.user_id == order_user.id, Order.order_date == target_date
    ).delete()

    new_orders = [
        Order(
            user_id=order_user.id,
            order_date=target_date,
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
    sync_all(db)

    return (
        db.query(Order)
        .filter(Order.user_id == order_user.id, Order.order_date == target_date)
        .all()
    )


@router.get("/my-week", response_model=list[OrderLineOut])
def my_week_orders(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return (
        db.query(Order)
        .filter(Order.user_id == user.id, Order.week_start == week_start())
        .order_by(Order.order_date, Order.item_name)
        .all()
    )


@router.get("/week/{username}", response_model=list[OrderLineOut])
def user_week_orders(
    username: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Anyone logged in can view anyone else's current-week orders -- backs
    the "Objednávám za" pill row on the order screen.

    Since 2026-09-11 any logged-in user can also SUBMIT on another existing
    user's behalf, via `on_behalf_of` on POST /orders. (This previously said
    submitting was admin-only through /admin/orders; that is no longer true.)
    """
    target_user = db.query(User).filter(User.username == username).first()
    if target_user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{username}' not found")
    return (
        db.query(Order)
        .filter(Order.user_id == target_user.id, Order.week_start == week_start())
        .order_by(Order.order_date, Order.item_name)
        .all()
    )
