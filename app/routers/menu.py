from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import MenuItem, User
from app.schemas import MenuItemOut
from app.timezone import today_local, week_start

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
        .filter(MenuItem.week_start == week_start(today), MenuItem.day == day_name)
        .order_by(MenuItem.category, MenuItem.item_name)
        .all()
    )
    return items


@router.get("/week", response_model=list[MenuItemOut])
def get_week_menu(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    items = (
        db.query(MenuItem)
        .filter(MenuItem.week_start == week_start())
        .order_by(MenuItem.day, MenuItem.category, MenuItem.item_name)
        .all()
    )
    return items
