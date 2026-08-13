"""Shared per-item grouping/date-formatting helpers, used by both
order_summary.py (email) and telegram_notify.py (Telegram) so the two
notification channels agree on how orders are grouped and dated."""

from collections import defaultdict
from datetime import date

from app.models import Order

DAY_NAMES_CZ = {
    0: "pondělí",
    1: "úterý",
    2: "středa",
    3: "čtvrtek",
    4: "pátek",
    5: "sobota",
    6: "neděle",
}


def format_date_cz(d: date) -> str:
    return f"{DAY_NAMES_CZ[d.weekday()]} {d.day}. {d.month}. {d.year}"


def group_by_item(rows: list[tuple[str, Order]]) -> dict[str, dict]:
    per_item: dict[str, dict] = defaultdict(lambda: {"qty": 0, "buyers": [], "notes": []})
    for username, order in rows:
        entry = per_item[order.item_name]
        entry["qty"] += order.quantity
        entry["buyers"].append(f"{username} ×{order.quantity}")
        if order.note:
            entry["notes"].append(f"{order.note} ({username})")
    return per_item
