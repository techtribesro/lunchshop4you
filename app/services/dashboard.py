"""Dashboard aggregation, shared between the /dashboard API endpoint and
the Google Sheets reporting mirror so both compute totals the same way."""

from collections import defaultdict

from sqlalchemy.orm import Session

from app.models import MenuItem, Order, User
from app.schemas import DashboardResponse, DashboardRow
from app.timezone import month_start, today_local, week_start

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def compute_dashboard(db: Session) -> DashboardResponse:
    current_week_start = week_start()
    current_month_start = month_start()

    users_by_id = {u.id: u.username for u in db.query(User).all()}

    month_orders = (
        db.query(Order)
        .filter(Order.order_date >= current_month_start, Order.order_date <= today_local())
        .all()
    )

    relevant_weeks = {order.week_start for order in month_orders}
    calories_by_key = {
        (item.week_start, item.day, item.item_name): item.calories_kcal
        for item in db.query(MenuItem).filter(MenuItem.week_start.in_(relevant_weeks)).all()
    } if relevant_weeks else {}

    def order_calories(order: Order) -> int:
        if order.order_date.weekday() >= 5:
            return 0
        day_name = DAY_NAMES[order.order_date.weekday()]
        kcal = calories_by_key.get((order.week_start, day_name, order.item_name))
        return (kcal or 0) * order.quantity

    week_total_by_user: dict[int, int] = defaultdict(int)
    month_total_by_user: dict[int, int] = defaultdict(int)
    week_kcal_by_user: dict[int, int] = defaultdict(int)
    month_kcal_by_user: dict[int, int] = defaultdict(int)
    daily_totals: dict[tuple[int, object], list[int]] = defaultdict(lambda: [0, 0, 0])  # [items, czk, kcal]

    for order in month_orders:
        line_total = order.quantity * order.unit_price_czk
        line_kcal = order_calories(order)

        month_total_by_user[order.user_id] += line_total
        month_kcal_by_user[order.user_id] += line_kcal
        if order.week_start == current_week_start:
            week_total_by_user[order.user_id] += line_total
            week_kcal_by_user[order.user_id] += line_kcal

        key = (order.user_id, order.order_date)
        daily_totals[key][0] += order.quantity
        daily_totals[key][1] += line_total
        daily_totals[key][2] += line_kcal

    rows = [
        DashboardRow(
            user=users_by_id.get(user_id, "unknown"),
            order_date=order_date,
            items_ordered=items,
            daily_total_czk=czk,
            week_total_czk=week_total_by_user[user_id],
            month_total_czk=month_total_by_user[user_id],
            daily_total_kcal=kcal,
            week_total_kcal=week_kcal_by_user[user_id],
            month_total_kcal=month_kcal_by_user[user_id],
        )
        for (user_id, order_date), (items, czk, kcal) in sorted(daily_totals.items(), key=lambda kv: kv[0][1])
    ]

    return DashboardResponse(
        rows=rows,
        week_aggregate_czk=sum(week_total_by_user.values()),
        month_aggregate_czk=sum(month_total_by_user.values()),
        week_aggregate_kcal=sum(week_kcal_by_user.values()),
        month_aggregate_kcal=sum(month_kcal_by_user.values()),
    )
