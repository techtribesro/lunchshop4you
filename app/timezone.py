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


def menu_target_week_start(for_date: date | None = None) -> date:
    """Which week's Monday a menu refresh happening on for_date should
    store items under. Weekdays (Mon-Fri) target the current week -- the
    common case is fixing/reloading the menu that's already active. A
    weekend call (Sat/Sun) targets the *upcoming* week instead: week_start()
    on a Sunday resolves to the Monday of the week that's ending that day,
    not the one about to start, so the normal Sunday-evening poll would
    otherwise silently store next week's menu under the wrong (outgoing)
    week_start."""
    for_date = for_date or today_local()
    if for_date.weekday() >= 5:
        return week_start(for_date) + timedelta(days=7)
    return week_start(for_date)


def month_start(for_date: date | None = None) -> date:
    for_date = for_date or today_local()
    return for_date.replace(day=1)


def next_business_day(for_date: date | None = None) -> date:
    """The next Mon-Fri date after for_date (skips weekends) -- e.g. Friday
    -> Monday, so "open ordering for tomorrow" is still useful on a Friday."""
    for_date = for_date or today_local()
    next_day = for_date + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day
