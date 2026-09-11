"""Regression: the week-read endpoints must agree with the week POST writes.

REAL PRODUCTION BUG (2026-09-11). `POST /orders` stores `Order.week_start`
for the *target* date, which on a Saturday or Sunday is the Monday of the
UPCOMING week. Both read endpoints filtered on `week_start()` with no
argument, which resolves to the Monday of the CURRENT (outgoing) week. On a
weekend a user submitted an order successfully and then could not see it:
`GET /orders/my-week` and `GET /orders/week/{username}` both returned [].

These tests pass an explicit weekend date rather than relying on when the
suite happens to run, so they fail against the unfixed code on ANY day --
they do not need a clock pin to bite (though the suite is also run under
forced Sat/Sun pins for this fix).
"""

from datetime import date, timedelta

import pytest

from app.models import MenuItem
from tests.conftest import DAY_NAMES, USER_PASSWORD, login_as

# A Saturday and the Sunday after it. The Monday these roll forward to is
# 2026-09-14, a DIFFERENT week from week_start() on either day (2026-09-07).
SATURDAY = date(2026, 9, 12)
SUNDAY = date(2026, 9, 13)
UPCOMING_MONDAY = date(2026, 9, 14)


@pytest.fixture
def pinned_weekend(request, monkeypatch):
    """Force today_local() to a weekend date at every import site.

    `app.routers.orders` does `from app.timezone import today_local, week_start`,
    so rebinding only `app.timezone` would leave the router's own references
    live. Patch the router module too.
    """
    pinned: date = request.param

    def _today():
        return pinned

    monkeypatch.setattr("app.timezone.today_local", _today)
    monkeypatch.setattr("app.routers.orders.today_local", _today)
    return pinned


@pytest.fixture
def upcoming_week_menu(db):
    """Seed the menu for the week the weekend order actually targets."""
    items = []
    for day in DAY_NAMES:
        items.append(
            MenuItem(
                week_start=UPCOMING_MONDAY,
                day=day,
                category="Polevka",
                item_name=f"{day} soup",
                description="test soup",
                price_czk=35,
                calories_kcal=120,
            )
        )
    db.add_all(items)
    db.commit()
    return items


@pytest.mark.parametrize(
    "pinned_weekend", [SATURDAY, SUNDAY], indirect=True, ids=["saturday", "sunday"]
)
def test_weekend_order_is_readable_from_my_week(
    pinned_weekend, upcoming_week_menu, logged_in_client, user
):
    """Submit on a weekend for the upcoming Monday, then read it back."""
    resp = logged_in_client.post(
        "/orders",
        json={
            "order_date": UPCOMING_MONDAY.isoformat(),
            "items": [{"item_name": "Monday soup", "quantity": 1, "note": ""}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["order_date"] == UPCOMING_MONDAY.isoformat()

    week = logged_in_client.get("/orders/my-week")
    assert week.status_code == 200, week.text
    rows = week.json()
    assert [row["item_name"] for row in rows] == ["Monday soup"], (
        "order submitted on a weekend for the upcoming Monday was not "
        f"returned by /orders/my-week; got {rows!r}"
    )


@pytest.mark.parametrize(
    "pinned_weekend", [SATURDAY, SUNDAY], indirect=True, ids=["saturday", "sunday"]
)
def test_weekend_order_is_readable_from_user_week(
    pinned_weekend, upcoming_week_menu, logged_in_client, user, other_user, client
):
    """The same row must be visible through /orders/week/{username}, which
    backs the "Objednavam za" pill row."""
    resp = logged_in_client.post(
        "/orders",
        json={
            "order_date": UPCOMING_MONDAY.isoformat(),
            # UPCOMING_MONDAY is a Monday, and the menu lookup filters on that
            # weekday's rows, so the line must be Monday's item.
            "items": [{"item_name": "Monday soup", "quantity": 2, "note": "no cream"}],
        },
    )
    assert resp.status_code == 200, resp.text

    week = logged_in_client.get(f"/orders/week/{user.username}")
    assert week.status_code == 200, week.text
    rows = week.json()
    assert [row["item_name"] for row in rows] == ["Monday soup"], (
        "order submitted on a weekend was not returned by "
        f"/orders/week/{user.username}; got {rows!r}"
    )
    assert rows[0]["quantity"] == 2
    assert rows[0]["note"] == "no cream"
