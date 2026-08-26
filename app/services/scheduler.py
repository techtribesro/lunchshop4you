import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.models import MenuItem
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.sheets_sync import sync_all
from app.timezone import menu_target_week_start

logger = logging.getLogger("scheduler")


def _weekly_menu_job() -> None:
    db = SessionLocal()
    try:
        refresh_menu(db)
        sync_all(db)
    except EmailPollError as exc:
        logger.error("Weekly menu poll failed: %s", exc)
    finally:
        db.close()


def _menu_gap_check_job() -> None:
    """Safety net for the weekly Sunday poll: if the vendor's email was
    late, missing, or failed to parse, this retries daily so the menu isn't
    stuck empty for the rest of the week. No-ops if the target week's menu
    is already loaded."""
    db = SessionLocal()
    try:
        current_week = menu_target_week_start()
        has_menu = db.query(MenuItem).filter(MenuItem.week_start == current_week).first() is not None
        if has_menu:
            return
        logger.info("No menu found for week of %s, attempting refresh", current_week)
        refresh_menu(db)
        sync_all(db)
    except EmailPollError as exc:
        logger.error("Daily menu gap-check poll failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    """The daily order summary (email + Telegram) is on-demand only --
    triggered manually from the admin panel, whenever the admin is actually
    ready to send, rather than firing automatically at a fixed
    cutoff-adjacent time."""
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    # BackgroundScheduler's own `timezone` only affects the jobstore, not
    # cron field interpretation -- CronTrigger needs `timezone=` explicitly,
    # or it silently falls back to the machine's system timezone (UTC on
    # Fly.io).
    #
    # Sunday 18:00 Prague time: menu emails typically land before the work week starts.
    scheduler.add_job(
        _weekly_menu_job, CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=settings.timezone)
    )
    # Every morning before the workday starts, retry if that Sunday poll
    # didn't leave a menu in place.
    scheduler.add_job(
        _menu_gap_check_job, CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=settings.timezone)
    )
    scheduler.start()
    return scheduler
