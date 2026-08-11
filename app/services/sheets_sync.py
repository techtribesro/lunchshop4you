"""One-way reporting mirror: pushes menu/orders/dashboard snapshots from
SQLite to Google Sheets for manual audit. The app never reads this data
back from Sheets -- SQLite is the source of truth (see REQUIREMENTS.md
revision note).
"""

import json
import logging
import os

from sqlalchemy.orm import Session

from app.config import settings
from app.models import MenuItem, Order, User
from app.services.dashboard import compute_dashboard

logger = logging.getLogger("sheets_sync")

TAB_NAMES = ["menu", "orders", "dashboard"]

_service = None


def _load_credentials():
    """The service account key can be provided either as a path to a JSON
    file (local dev, mounted volume) or as inline JSON in the env var
    (Fly.io secrets are env-only, no secret file mounts)."""
    from google.oauth2 import service_account

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = settings.google_service_account_json.strip()

    if raw.startswith("{"):
        return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    if os.path.exists(raw):
        return service_account.Credentials.from_service_account_file(raw, scopes=scopes)
    return None


def _get_service():
    global _service
    if _service is not None:
        return _service

    if not settings.google_sheets_spreadsheet_id:
        logger.warning("Google Sheets sync not configured (no spreadsheet id); skipping sync")
        return None

    credentials = _load_credentials()
    if credentials is None:
        logger.warning("Google Sheets sync not configured (no service account credentials); skipping sync")
        return None

    from googleapiclient.discovery import build

    _service = build("sheets", "v4", credentials=credentials)
    return _service


def _ensure_tab_exists(service, spreadsheet_id: str, title: str) -> None:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if title in existing:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()


def _write_sheet(sheet_name: str, header: list[str], rows: list[list]) -> None:
    service = _get_service()
    if service is None:
        return

    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_tab_exists(service, spreadsheet_id, sheet_name)

    # Clear first -- a plain values().update() only overwrites the cells it
    # addresses, so a sync with fewer rows than last time would otherwise
    # leave stale trailing rows behind.
    service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=sheet_name).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        body={"values": [header] + rows},
    ).execute()


def sync_menu(db: Session) -> None:
    items = db.query(MenuItem).order_by(MenuItem.week_start, MenuItem.day, MenuItem.category).all()
    rows = [
        [
            i.week_start.isoformat(), i.day, i.category, i.item_name, i.description,
            i.price_czk, i.calories_kcal, i.parsed_at.isoformat(),
        ]
        for i in items
    ]
    _write_sheet(
        "menu",
        ["week_start", "day", "category", "item_name", "description", "price_czk", "calories_kcal", "parsed_at"],
        rows,
    )


def sync_orders(db: Session) -> None:
    users_by_id = {u.id: u.username for u in db.query(User).all()}
    orders = db.query(Order).order_by(Order.order_date, Order.user_id).all()
    rows = [
        [
            users_by_id.get(o.user_id, "unknown"),
            o.order_date.isoformat(),
            o.item_name,
            o.quantity,
            o.unit_price_czk,
            o.note,
            o.submitted_at.isoformat(),
            o.week_start.isoformat(),
        ]
        for o in orders
    ]
    _write_sheet(
        "orders",
        ["user", "order_date", "item_name", "quantity", "unit_price_czk", "note", "submitted_at", "week_start"],
        rows,
    )


def sync_dashboard(db: Session) -> None:
    data = compute_dashboard(db)
    rows = [
        [
            r.user,
            r.order_date.isoformat(),
            r.items_ordered,
            r.daily_total_czk,
            r.week_total_czk,
            r.month_total_czk,
            r.daily_total_kcal,
            r.week_total_kcal,
            r.month_total_kcal,
        ]
        for r in data.rows
    ]
    rows.append(
        [
            "TOTAL", "", "", "",
            data.week_aggregate_czk, data.month_aggregate_czk,
            "", data.week_aggregate_kcal, data.month_aggregate_kcal,
        ]
    )
    _write_sheet(
        "dashboard",
        [
            "user", "order_date", "items_ordered",
            "daily_total_czk", "week_total_czk", "month_total_czk",
            "daily_total_kcal", "week_total_kcal", "month_total_kcal",
        ],
        rows,
    )


def sync_all(db: Session) -> None:
    try:
        sync_menu(db)
        sync_orders(db)
        sync_dashboard(db)
    except Exception:
        logger.exception("Google Sheets sync failed")
