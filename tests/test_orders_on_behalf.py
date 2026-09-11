"""Authorization tests for submitting an order on another user's behalf.

Operator decision 2026-09-11: this is no longer admin-only. Any logged-in
user may submit for another EXISTING registered user via `on_behalf_of` on
POST /orders. These tests pin that privilege change down, plus the guards
that deliberately survive it (unknown user -> 404, anonymous -> 401, and the
unchanged `_check_ordering_allowed` date rules).

The overwrite behaviour asserted in `test_on_behalf_submit_overwrites...` is
an ACCEPTED RISK, not a defect -- it is tested to document the decision, so a
later change cannot silently alter it without turning a test red.
"""

from datetime import date, timedelta

from app.models import Order

from .conftest import USER_PASSWORD, login_as


def _line(day_date: date, index: int = 1) -> dict:
    """A valid order line for the seeded `menu_week` fixture, whose item names
    are '<EnglishDayName> main <index>'."""
    day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][day_date.weekday()]
    return {"item_name": f"{day_name} main {index}", "quantity": 1, "note": ""}


def _orders_for(db, user_id: int, when: date) -> list[Order]:
    return (
        db.query(Order)
        .filter(Order.user_id == user_id, Order.order_date == when)
        .order_by(Order.item_name)
        .all()
    )


def test_non_admin_can_submit_for_another_user(
    logged_in_client, db, user, other_user, menu_week, next_weekday, stub_sheets_sync
):
    """THE privilege change: a non-admin submits for a colleague and the rows
    land under the TARGET user's id, not the submitter's."""
    assert user.is_admin is False, "this test is only meaningful for a non-admin"

    response = logged_in_client.post(
        "/orders",
        json={
            "items": [_line(next_weekday)],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": other_user.username,
        },
    )

    assert response.status_code == 200, response.text

    # Rows belong to the target...
    target_rows = _orders_for(db, other_user.id, next_weekday)
    assert [row.item_name for row in target_rows] == [_line(next_weekday)["item_name"]]
    # ...and emphatically not to the submitter.
    assert _orders_for(db, user.id, next_weekday) == []

    # Sheets mirror still fires after a successful on-behalf submit.
    assert len(stub_sheets_sync) == 1


def test_on_behalf_note_persists_under_target_user(
    logged_in_client, db, other_user, menu_week, next_weekday, stub_sheets_sync
):
    line = _line(next_weekday) | {"note": "bez cibule"}
    response = logged_in_client.post(
        "/orders",
        json={
            "items": [line],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": other_user.username,
        },
    )

    assert response.status_code == 200, response.text
    rows = _orders_for(db, other_user.id, next_weekday)
    assert [row.note for row in rows] == ["bez cibule"]


def test_on_behalf_submit_overwrites_target_existing_order(
    client, db, user, other_user, menu_week, next_weekday, stub_sheets_sync
):
    """ACCEPTED RISK, asserted deliberately: the target's own earlier order for
    that date is replaced, not merged. Operator was shown this and accepted it."""
    # The colleague orders for themselves first.
    colleague_client = login_as(client, other_user.username, USER_PASSWORD)
    first = colleague_client.post(
        "/orders",
        json={"items": [_line(next_weekday, 1)], "order_date": next_weekday.isoformat()},
    )
    assert first.status_code == 200, first.text
    assert len(_orders_for(db, other_user.id, next_weekday)) == 1

    # A different non-admin then submits on their behalf with a different item.
    submitter_client = login_as(client, user.username, USER_PASSWORD)
    second = submitter_client.post(
        "/orders",
        json={
            "items": [_line(next_weekday, 2)],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": other_user.username,
        },
    )
    assert second.status_code == 200, second.text

    rows = _orders_for(db, other_user.id, next_weekday)
    assert [row.item_name for row in rows] == [_line(next_weekday, 2)["item_name"]]


def test_on_behalf_unknown_username_returns_404(
    logged_in_client, db, user, menu_week, next_weekday, stub_sheets_sync
):
    """Existing registered users ONLY -- no guest names, no free text."""
    response = logged_in_client.post(
        "/orders",
        json={
            "items": [_line(next_weekday)],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": "no-such-person",
        },
    )

    assert response.status_code == 404, response.text
    # Nothing was written for anyone, and no sync fired.
    assert db.query(Order).count() == 0
    assert stub_sheets_sync == []


def test_on_behalf_submit_rejects_anonymous(client, other_user, menu_week, next_weekday):
    """Opening this up to all *logged-in* users must not open it to the world."""
    response = client.post(
        "/orders",
        json={
            "items": [_line(next_weekday)],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": other_user.username,
        },
    )

    assert response.status_code == 401, response.text


def _past_date() -> date:
    return date(2020, 1, 6)  # a Monday, safely in the past


def _weekend_date(next_weekday: date) -> date:
    """The coming Saturday relative to an orderable weekday -- future, so the
    400 can only come from the weekend rule, never the past-date rule."""
    return next_weekday + timedelta(days=5 - next_weekday.weekday())


def test_past_date_rejected_for_self_and_on_behalf(
    logged_in_client, other_user, menu_week, stub_sheets_sync
):
    """`_check_ordering_allowed` is unchanged and applies to on-behalf submits
    exactly as to self-submits."""
    past = _past_date()
    base = {"items": [_line(past)], "order_date": past.isoformat()}

    for payload in (base, base | {"on_behalf_of": other_user.username}):
        response = logged_in_client.post("/orders", json=payload)
        assert response.status_code == 400, (payload, response.text)
        assert "past date" in response.json()["detail"].lower()


def test_weekend_rejected_for_self_and_on_behalf(
    logged_in_client, other_user, menu_week, next_weekday, stub_sheets_sync
):
    weekend = _weekend_date(next_weekday)
    assert weekend.weekday() >= 5
    base = {"items": [_line(next_weekday)], "order_date": weekend.isoformat()}

    for payload in (base, base | {"on_behalf_of": other_user.username}):
        response = logged_in_client.post("/orders", json=payload)
        assert response.status_code == 400, (payload, response.text)
        assert "weekend" in response.json()["detail"].lower()


def test_self_submit_path_unchanged(
    logged_in_client, db, user, other_user, menu_week, next_weekday, stub_sheets_sync
):
    """Omitting `on_behalf_of` behaves exactly as before the change."""
    response = logged_in_client.post(
        "/orders",
        json={"items": [_line(next_weekday)], "order_date": next_weekday.isoformat()},
    )

    assert response.status_code == 200, response.text
    assert len(_orders_for(db, user.id, next_weekday)) == 1
    assert _orders_for(db, other_user.id, next_weekday) == []
    assert len(stub_sheets_sync) == 1


def test_on_behalf_of_self_is_equivalent_to_self_submit(
    logged_in_client, db, user, menu_week, next_weekday, stub_sheets_sync
):
    """Naming yourself as the target is a no-op, not a 404 or a duplicate."""
    response = logged_in_client.post(
        "/orders",
        json={
            "items": [_line(next_weekday)],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": user.username,
        },
    )

    assert response.status_code == 200, response.text
    assert len(_orders_for(db, user.id, next_weekday)) == 1
