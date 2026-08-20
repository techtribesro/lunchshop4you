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


HEADER_BG = {"red": 0.16, "green": 0.29, "blue": 0.49}
HEADER_FG = {"red": 1, "green": 1, "blue": 1}
TOTAL_BG = {"red": 0.9, "green": 0.93, "blue": 0.98}


def _ensure_tab_exists(service, spreadsheet_id: str, title: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    response = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def _apply_formatting(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    num_cols: int,
    num_data_rows: int,
    currency_cols: tuple[int, ...],
    kcal_cols: tuple[int, ...],
    bold_last_row: bool,
    column_widths: dict[int, int] | None = None,
) -> None:
    requests = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_BG,
                        "textFormat": {"foregroundColor": HEADER_FG, "bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
    ]

    if column_widths:
        # Explicit widths, not autoResizeDimensions -- autoResize measures
        # plain (non-bold) text width, so it under-sizes bold header cells
        # by a character or two and clips them (e.g. "daily_total_czk" ->
        # "daily_total_czl"). Fixed widths sized for these short labels
        # sidestep that entirely.
        for col, width in column_widths.items():
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id, "dimension": "COLUMNS",
                            "startIndex": col, "endIndex": col + 1,
                        },
                        "properties": {"pixelSize": width},
                        "fields": "pixelSize",
                    }
                }
            )
    else:
        requests.append(
            {
                "autoResizeDimensions": {
                    "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": num_cols}
                }
            }
        )

    data_end_row = num_data_rows + 1  # +1 for the header row already occupying row 0
    for col in (*currency_cols, *kcal_cols):
        pattern = '#,##0 "Kč"' if col in currency_cols else '#,##0 "kcal"'
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": data_end_row,
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

    if bold_last_row and num_data_rows > 0:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id, "startRowIndex": num_data_rows, "endRowIndex": data_end_row,
                        "startColumnIndex": 0, "endColumnIndex": num_cols,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": TOTAL_BG, "textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }
        )

    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


def _write_sheet(
    sheet_name: str,
    header: list[str],
    rows: list[list],
    *,
    currency_cols: tuple[int, ...] = (),
    kcal_cols: tuple[int, ...] = (),
    bold_last_row: bool = False,
    column_widths: dict[int, int] | None = None,
) -> None:
    service = _get_service()
    if service is None:
        return

    spreadsheet_id = settings.google_sheets_spreadsheet_id
    sheet_id = _ensure_tab_exists(service, spreadsheet_id, sheet_name)

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

    _apply_formatting(
        service, spreadsheet_id, sheet_id, len(header), len(rows),
        currency_cols, kcal_cols, bold_last_row, column_widths,
    )


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
        currency_cols=(5,),
        kcal_cols=(6,),
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
        currency_cols=(4,),
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
            "CELKEM", "", "", "",
            data.week_aggregate_czk, data.month_aggregate_czk,
            "", data.week_aggregate_kcal, data.month_aggregate_kcal,
        ]
    )
    _write_sheet(
        "dashboard",
        [
            "Uživatel", "Datum", "Položky",
            "Den Kč", "Týden Kč", "Měsíc Kč",
            "Den kcal", "Týden kcal", "Měsíc kcal",
        ],
        rows,
        currency_cols=(3, 4, 5),
        kcal_cols=(6, 7, 8),
        bold_last_row=True,
        column_widths={0: 100, 1: 100, 2: 70, 3: 90, 4: 95, 5: 95, 6: 95, 7: 100, 8: 100},
    )


def sync_all(db: Session) -> None:
    try:
        sync_menu(db)
        sync_orders(db)
        sync_dashboard(db)
    except Exception:
        logger.exception("Google Sheets sync failed")
