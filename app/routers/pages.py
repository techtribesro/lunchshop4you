from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.db import get_db
from app.models import MenuItem, Order, User
from app.timezone import today_local, week_start

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
DAY_LABELS_CZ = {
    "Monday": "Pondělí",
    "Tuesday": "Úterý",
    "Wednesday": "Středa",
    "Thursday": "Čtvrtek",
    "Friday": "Pátek",
}


@router.get("/login")
def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    if user is not None:
        return RedirectResponse("/orders", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/orders")
def order_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    if user is None:
        return RedirectResponse("/login", status_code=303)

    today = today_local()
    ws = week_start(today)
    is_weekday = today.weekday() < 5

    week_items = (
        db.query(MenuItem)
        .filter(MenuItem.week_start == ws)
        .order_by(MenuItem.day, MenuItem.category, MenuItem.item_name)
        .all()
    )
    menu_by_day: dict[str, list[dict]] = {label: [] for label in DAY_LABELS_CZ.values()}
    for item in week_items:
        label = DAY_LABELS_CZ.get(item.day)
        if label is None:
            continue
        menu_by_day[label].append(
            {
                "cat": item.category,
                "name": item.item_name,
                "desc": item.description,
                "price": item.price_czk,
                "kcal": item.calories_kcal,
            }
        )

    today_label = DAY_LABELS_CZ.get(DAY_NAMES[today.weekday()]) if is_weekday else None
    calories_by_item_name = {item.item_name: item.calories_kcal for item in week_items}

    # No more day-locking: every weekday in the loaded week is orderable, so
    # existing orders are collected for the whole week (keyed by day label),
    # not just today.
    orders_by_day: dict[str, dict] = {label: {} for label in DAY_LABELS_CZ.values()}
    week_orders = db.query(Order).filter(Order.user_id == user.id, Order.week_start == ws).all()
    for order in week_orders:
        day_name = DAY_NAMES[order.order_date.weekday()]
        label = DAY_LABELS_CZ.get(day_name)
        if label is None:
            continue
        orders_by_day[label][order.item_name] = {
            "qty": order.quantity,
            "note": order.note,
            "price": order.unit_price_czk,
            "kcal": calories_by_item_name.get(order.item_name),
        }

    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "user": user,
            "menu_by_day": menu_by_day,
            "day_labels": list(DAY_LABELS_CZ.values()),
            "today_label": today_label,
            "orders_by_day": orders_by_day,
            "today_iso": today.isoformat(),
            "week_start_iso": ws.isoformat(),
        },
    )
