from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.order_summary import OrderSummaryError, send_daily_order_summary
from app.services.sheets_sync import sync_all

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/parse-menu")
def parse_menu(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    try:
        item_count = refresh_menu(db)
    except EmailPollError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    sync_all(db)
    return {"items_parsed": item_count}


@router.post("/send-order-summary")
def send_order_summary(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    try:
        line_count = send_daily_order_summary(db)
    except OrderSummaryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {"order_lines_sent": line_count}
