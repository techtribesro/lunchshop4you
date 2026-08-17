import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.sheets_sync import sync_all

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


def start_scheduler() -> BackgroundScheduler:
    """Only the weekly menu poll runs on a schedule. The daily order
    summary (email + Telegram) is on-demand only -- triggered manually
    from the admin panel, whenever the admin is actually ready to send,
    rather than firing automatically at a fixed cutoff-adjacent time."""
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
    scheduler.start()
    return scheduler
