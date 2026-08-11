from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Order, User
from app.schemas import DashboardResponse, DashboardRow
from app.timezone import month_start, today_local, week_start

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
def get_dashboard(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    current_week_start = week_start()
    current_month_start = month_start()

    users_by_id = {u.id: u.username for u in db.query(User).all()}

    month_orders = (
        db.query(Order)
        .filter(Order.order_date >= current_month_start, Order.order_date <= today_local())
        .all()
    )

    week_total_by_user: dict[int, int] = defaultdict(int)
    month_total_by_user: dict[int, int] = defaultdict(int)
    daily_totals: dict[tuple[int, object], list[int]] = defaultdict(lambda: [0, 0])  # [items, czk]

    for order in month_orders:
        line_total = order.quantity * order.unit_price_czk
        month_total_by_user[order.user_id] += line_total
        if order.week_start == current_week_start:
            week_total_by_user[order.user_id] += line_total
        key = (order.user_id, order.order_date)
        daily_totals[key][0] += order.quantity
        daily_totals[key][1] += line_total

    rows = [
        DashboardRow(
            user=users_by_id.get(user_id, "unknown"),
            order_date=order_date,
            items_ordered=items,
            daily_total_czk=czk,
            week_total_czk=week_total_by_user[user_id],
            month_total_czk=month_total_by_user[user_id],
        )
        for (user_id, order_date), (items, czk) in sorted(daily_totals.items(), key=lambda kv: kv[0][1])
    ]

    return DashboardResponse(
        rows=rows,
        week_aggregate_czk=sum(week_total_by_user.values()),
        month_aggregate_czk=sum(month_total_by_user.values()),
    )
