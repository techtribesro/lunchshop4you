from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.config import settings
from app.db import get_db
from app.models import MenuItem, Order, User
from app.timezone import is_before_cutoff, now_local, today_local, week_start

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

WARN_WINDOW_MINUTES = 15


def _cutoff_state(is_weekday: bool) -> str:
    """"open" / "warn" (closing soon) / "closed", for the cutoff pill."""
    if not is_weekday:
        return "closed"
    now = now_local()
    cutoff_dt = now.replace(
        hour=settings.order_cutoff.hour, minute=settings.order_cutoff.minute, second=0, microsecond=0
    )
    if now >= cutoff_dt:
        return "closed"
    if cutoff_dt - now <= timedelta(minutes=WARN_WINDOW_MINUTES):
        return "warn"
    return "open"


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
            }
        )

    today_label = DAY_LABELS_CZ.get(DAY_NAMES[today.weekday()]) if is_weekday else None

    existing_orders = {
        order.item_name: {"qty": order.quantity, "note": order.note, "price": order.unit_price_czk}
        for order in db.query(Order).filter(Order.user_id == user.id, Order.order_date == today).all()
    }

    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "user": user,
            "menu_by_day": menu_by_day,
            "day_labels": list(DAY_LABELS_CZ.values()),
            "today_label": today_label,
            "existing_orders": existing_orders,
            "ordering_open": is_weekday and is_before_cutoff(),
            "cutoff_state": _cutoff_state(is_weekday),
            "cutoff_time": settings.order_cutoff_time,
            "today_iso": today.isoformat(),
        },
    )
