from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import MenuItem, Order, User
from app.schemas import OrderLineOut, OrderSubmitRequest
from app.timezone import is_before_cutoff, today_local, week_start

router = APIRouter(prefix="/orders", tags=["orders"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@router.post("", response_model=list[OrderLineOut])
def submit_order(
    payload: OrderSubmitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not is_before_cutoff():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Order cutoff has passed for today")

    today = today_local()
    if today.weekday() >= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No ordering on weekends")

    day_name = DAY_NAMES[today.weekday()]
    todays_week_start = week_start(today)

    menu_by_name = {
        item.item_name: item
        for item in db.query(MenuItem)
        .filter(MenuItem.week_start == todays_week_start, MenuItem.day == day_name)
        .all()
    }

    for line in payload.items:
        if line.item_name not in menu_by_name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"'{line.item_name}' is not on today's menu"
            )
        if line.quantity < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be at least 1")

    db.query(Order).filter(Order.user_id == user.id, Order.order_date == today).delete()

    new_orders = [
        Order(
            user_id=user.id,
            order_date=today,
            week_start=todays_week_start,
            item_name=line.item_name,
            quantity=line.quantity,
            unit_price_czk=menu_by_name[line.item_name].price_czk,
            note=line.note.strip()[:255],
        )
        for line in payload.items
    ]
    db.add_all(new_orders)
    db.commit()

    return db.query(Order).filter(Order.user_id == user.id, Order.order_date == today).all()


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
