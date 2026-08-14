import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.services.email_poller import EmailPollError, refresh_menu
from app.services.order_summary import OrderSummaryError, send_daily_order_summary
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


def _daily_order_summary_job() -> None:
    db = SessionLocal()
    try:
        send_daily_order_summary(db)
    except OrderSummaryError as exc:
        logger.error("Daily order summary failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    # BackgroundScheduler's own `timezone` only affects the jobstore, not
    # cron field interpretation -- each CronTrigger needs `timezone=`
    # explicitly, or it silently falls back to the machine's system
    # timezone (UTC on Fly.io), which is how the 11:05 job ended up firing
    # at 13:05 Prague time (11:05 UTC) instead.
    #
    # Sunday 18:00 Prague time: menu emails typically land before the work week starts.
    scheduler.add_job(
        _weekly_menu_job, CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=settings.timezone)
    )
    # Shortly after the order cutoff, Mon-Fri: gives a few minutes' buffer
    # for last-second edits to settle before the restaurant is emailed.
    summary_time = settings.order_summary_time
    scheduler.add_job(
        _daily_order_summary_job,
        CronTrigger(
            day_of_week="mon-fri", hour=summary_time.hour, minute=summary_time.minute, timezone=settings.timezone
        ),
    )
    scheduler.start()
    return scheduler
