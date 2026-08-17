from datetime import date

from app.timezone import menu_target_week_start, month_start, week_start


def test_week_start_is_monday():
    assert week_start(date(2026, 8, 13)) == date(2026, 8, 10)  # Thursday -> Monday
    assert week_start(date(2026, 8, 10)) == date(2026, 8, 10)  # Monday -> itself


def test_month_start():
    assert month_start(date(2026, 8, 27)) == date(2026, 8, 1)


def test_menu_target_week_start_weekday_targets_current_week():
    assert menu_target_week_start(date(2026, 8, 14)) == date(2026, 8, 10)  # Friday


def test_menu_target_week_start_weekend_targets_upcoming_week():
    assert menu_target_week_start(date(2026, 8, 15)) == date(2026, 8, 17)  # Saturday
    assert menu_target_week_start(date(2026, 8, 16)) == date(2026, 8, 17)  # Sunday
