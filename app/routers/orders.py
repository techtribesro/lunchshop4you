from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import EarlyOrderingWindow, MenuItem, Order, User
from app.schemas import OrderLineOut, OrderSubmitRequest
from app.timezone import today_local, week_start

router = APIRouter(prefix="/orders", tags=["orders"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _check_ordering_allowed(db: Session, target_date) -> None:
    """No time-of-day cutoff -- orders for today or an admin-opened future
    date can be placed/edited any time. Only past dates and weekends are
    blocked."""
    today = today_local()

    if target_date < today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot order for a past date")

    if target_date != today:
        window = db.query(EarlyOrderingWindow).filter(EarlyOrderingWindow.order_date == target_date).first()
        if window is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Ordering for {target_date} is not open yet")

    if target_date.weekday() >= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No ordering on weekends")


@router.post("", response_model=list[OrderLineOut])
def submit_order(
    payload: OrderSubmitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    target_date = payload.order_date or today_local()
    _check_ordering_allowed(db, target_date)

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

    db.query(Order).filter(Order.user_id == user.id, Order.order_date == target_date).delete()

    new_orders = [
        Order(
            user_id=user.id,
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

    return db.query(Order).filter(Order.user_id == user.id, Order.order_date == target_date).all()


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
