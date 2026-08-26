from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import MenuItem, User
from app.schemas import MenuItemOut
from app.timezone import today_local
from app.timezone import week_start as current_week_start

router = APIRouter(prefix="/menu", tags=["menu"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@router.get("/today", response_model=list[MenuItemOut])
def get_today_menu(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    today = today_local()
    if today.weekday() >= 5:
        return []
    day_name = DAY_NAMES[today.weekday()]
    items = (
        db.query(MenuItem)
        .filter(MenuItem.week_start == current_week_start(today), MenuItem.day == day_name)
        .order_by(MenuItem.category, MenuItem.item_name)
        .all()
    )
    return items


@router.get("/week", response_model=list[MenuItemOut])
def get_week_menu(
    week_start: date | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Defaults to the current week when week_start isn't given, same as
    before this took a parameter at all -- but previously the parameter
    didn't exist on this route (it was silently ignored by FastAPI, since
    an undeclared query string param isn't an error), so a caller passing
    ?week_start=<some other week> always got the current week back
    regardless, with no indication anything was wrong. There was no way to
    actually view a week other than the current one through this endpoint."""
    target = week_start or current_week_start()
    items = (
        db.query(MenuItem)
        .filter(MenuItem.week_start == target)
        .order_by(MenuItem.day, MenuItem.category, MenuItem.item_name)
        .all()
    )
    return items
