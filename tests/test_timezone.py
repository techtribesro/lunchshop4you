from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.timezone import PRAGUE, is_before_cutoff, month_start, week_start


def test_is_before_cutoff_true_before_1100():
    moment = datetime(2026, 8, 11, 10, 59, tzinfo=PRAGUE)
    assert is_before_cutoff(moment) is True


def test_is_before_cutoff_false_at_or_after_1100():
    moment = datetime(2026, 8, 11, 11, 0, tzinfo=PRAGUE)
    assert is_before_cutoff(moment) is False


def test_is_before_cutoff_handles_other_timezones():
    # 09:05 UTC == 11:05 CEST (Prague, summer) -- past cutoff
    moment = datetime(2026, 8, 11, 9, 5, tzinfo=ZoneInfo("UTC"))
    assert is_before_cutoff(moment) is False


def test_week_start_is_monday():
    assert week_start(date(2026, 8, 13)) == date(2026, 8, 10)  # Thursday -> Monday
    assert week_start(date(2026, 8, 10)) == date(2026, 8, 10)  # Monday -> itself


def test_month_start():
    assert month_start(date(2026, 8, 27)) == date(2026, 8, 1)
