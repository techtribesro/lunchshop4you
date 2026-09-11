import os

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

# Cache-busting query param for /static/css/app.css -- StaticFiles doesn't
# set Cache-Control, so browsers can hang onto a stale copy across deploys
# unless the URL itself changes. Computed once at startup from the file's
# mtime, so every deploy (which touches the file) gets a fresh value.
_CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "css", "app.css")
CSS_VERSION = str(int(os.path.getmtime(_CSS_PATH)))

# Where a successful login (and a bare "/") sends the user. This is the mode
# chooser, not the order grid: /orders stays directly reachable as a deep link.
# Kept as a constant because main.py's root() references the same destination --
# login.html's submit handler has its own copy in JS, which cannot import this
# and must be kept in step by hand.
POST_LOGIN_PATH = "/modes"

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
        return RedirectResponse(POST_LOGIN_PATH, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"css_version": CSS_VERSION})


@router.get("/modes")
def modes_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    """The mode chooser that sits between login and the order screen.

    Deliberately NOT a one-time gate: it is a plain GET any logged-in user can
    return to at any time (the order screen links back here), so it holds no
    state and sets no "already chosen" flag.
    """
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "modes.html",
        {"user": user, "css_version": CSS_VERSION},
    )


@router.get("/modes/weekly")
def weekly_prompt_placeholder(
    request: Request, user: User | None = Depends(get_current_user_optional)
):
    """Placeholder for Mode 1, the guided weekly prompt.

    The real flow is built in a later task; this exists so the chooser's Mode 1
    card links somewhere that returns 200 instead of 404. The path is the one
    the finished prompt will take over, so the chooser needs no edit then.
    """
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "weekly_placeholder.html",
        {"user": user, "css_version": CSS_VERSION},
    )


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
            "css_version": CSS_VERSION,
        },
    )
