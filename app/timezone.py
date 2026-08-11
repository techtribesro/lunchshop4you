from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

PRAGUE = ZoneInfo(settings.timezone)


def now_local() -> datetime:
    return datetime.now(PRAGUE)


def now_local_naive() -> datetime:
    """Naive local timestamp for DB storage (SQLite doesn't round-trip tzinfo)."""
    return datetime.now(PRAGUE).replace(tzinfo=None)


def today_local() -> date:
    return now_local().date()


def is_before_cutoff(moment: datetime | None = None) -> bool:
    moment = moment or now_local()
    return moment.astimezone(PRAGUE).time() < settings.order_cutoff


def week_start(for_date: date | None = None) -> date:
    """Monday of the ISO week containing for_date (defaults to today, local time)."""
    for_date = for_date or today_local()
    return for_date - timedelta(days=for_date.weekday())


def month_start(for_date: date | None = None) -> date:
    for_date = for_date or today_local()
    return for_date.replace(day=1)
