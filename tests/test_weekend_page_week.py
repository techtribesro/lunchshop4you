"""Regression: the PAGE routes must render the week POST /orders accepts.

REAL PRODUCTION BUG (2026-09-13), operator's words: "i cant chose anything
from the menu which is what i could".

`app/routers/pages.py` renders `week_start(today)` (:125 weekly prompt, :171
order grid) while `app/routers/orders.py` targets `menu_target_week_start()`
(:116, :142). On a Saturday or Sunday those resolve to DIFFERENT Mondays --
the outgoing week vs. the upcoming one -- so the page shows a full menu for a
week that ordering will not accept. Every rendered day comes back
`orderable=False`, and a POST for a displayed date is rejected 400
"Cannot order for a past date".

This is the same divergence class that `t16` fixed on the ORDER side in the
prior run; nobody applied it to the page routes. See REQUIREMENTS.md s9.

These tests pin the clock to a weekend rather than relying on when the suite
happens to run, and they seed ONLY the upcoming week (the week ordering
actually targets) so that "the page renders the orderable week" is the single
thing under test.

EXPECTED TO FAIL until t2 changes pages.py to use menu_target_week_start().
The failure is the deliverable: it should read `assert '2026-09-07' ==
'2026-09-14'`, i.e. the page rendering the outgoing week.

NOTE ON THE CLOCK PIN: this fixture is deliberately module-local. A global
autouse pin in tests/conftest.py does NOT work -- conftest's `menu_week`
fixture seeds `{week_start(today_local()), week_start(_next_orderable_weekday())}`
at fixture time, and a global pin desynchronises it from the rendered week,
producing ~24 spurious failures on patched and unpatched code alike. Keep the
pin here, and do not use `menu_week` in this module.
"""

from datetime import date, timedelta

import pytest

from app.models import MenuItem, Order
from app.timezone import menu_target_week_start, week_start
from tests.conftest import DAY_NAMES
from tests.test_weekly_prompt import extract_js_const

# A Saturday and the Sunday after it. Both roll forward to 2026-09-14, while
# week_start() on either day resolves to 2026-09-07 -- a different week.
SATURDAY = date(2026, 9, 12)
SUNDAY = date(2026, 9, 13)
UPCOMING_MONDAY = date(2026, 9, 14)
OUTGOING_MONDAY = date(2026, 9, 7)


@pytest.fixture
def pinned_weekend(request, monkeypatch):
    """Force today_local() to a weekend date at every import site on these
    routes' path.

    `pages.py`, `orders.py` and `menu.py` all do
    `from app.timezone import today_local, ...` at import, so rebinding only
    `app.timezone` would leave each module's own reference live --
    `app.routers.pages.today_local` in particular is mandatory, since that is
    what the rendered week is computed from.

    `week_start` and `menu_target_week_start` need NO patching: they take
    `for_date` and resolve their default through `app.timezone.today_local` at
    CALL time (app/timezone.py:24, :37), so they follow the patch.
    """
    pinned: date = request.param

    def _today():
        return pinned

    monkeypatch.setattr("app.timezone.today_local", _today)
    monkeypatch.setattr("app.routers.pages.today_local", _today)
    monkeypatch.setattr("app.routers.orders.today_local", _today)
    monkeypatch.setattr("app.routers.menu.today_local", _today)
    return pinned


@pytest.fixture
def upcoming_week_menu(db):
    """Seed ONLY the week the weekend order actually targets (2026-09-14).

    Deliberately not conftest's `menu_week`, which seeds both weeks and would
    mask the divergence by making the outgoing week render a full menu too.
    """
    items = [
        MenuItem(
            week_start=UPCOMING_MONDAY,
            day=day,
            category="Polevka",
            item_name=f"{day} soup",
            description="test soup",
            price_czk=35,
            calories_kcal=120,
        )
        for day in DAY_NAMES
    ]
    db.add_all(items)
    db.commit()
    return items


@pytest.mark.parametrize(
    "pinned_weekend", [SATURDAY, SUNDAY], indirect=True, ids=["saturday", "sunday"]
)
def test_weekly_page_renders_the_orderable_week(
    pinned_weekend, upcoming_week_menu, logged_in_client, user
):
    """The week /modes/weekly renders must equal the week ordering targets."""
    # Prove the pin actually bit before asserting anything about the page.
    assert week_start(None) == OUTGOING_MONDAY
    assert menu_target_week_start(None) == UPCOMING_MONDAY

    body = logged_in_client.get("/modes/weekly").text
    week = extract_js_const(body, "WEEK")

    rendered_dates = [day["date"] for day in week]
    orderable_flags = [day["orderable"] for day in week]
    item_counts = [len(day["items"]) for day in week]
    print(
        f"\nPINNED {pinned_weekend} (weekday={pinned_weekend.weekday()})"
        f"\n  week_start()             -> {week_start(None)}"
        f"\n  menu_target_week_start() -> {menu_target_week_start(None)}"
        f"\n  rendered dates           -> {rendered_dates}"
        f"\n  orderable flags          -> {orderable_flags}"
        f"\n  items per day            -> {item_counts}"
    )

    assert rendered_dates[0] == UPCOMING_MONDAY.isoformat(), (
        "the weekly page rendered a different week than POST /orders accepts: "
        f"page starts {rendered_dates[0]}, ordering targets "
        f"{UPCOMING_MONDAY.isoformat()}"
    )
    assert rendered_dates == [
        (UPCOMING_MONDAY + timedelta(days=i)).isoformat() for i in range(5)
    ]
    assert all(item_counts), f"some rendered days had no dishes: {item_counts}"
    assert all(orderable_flags), (
        f"the page showed dishes that are not orderable: {orderable_flags}"
    )


@pytest.mark.parametrize(
    "pinned_weekend", [SATURDAY, SUNDAY], indirect=True, ids=["saturday", "sunday"]
)
def test_dish_shown_as_orderable_can_actually_be_ordered(
    pinned_weekend, upcoming_week_menu, logged_in_client, user, db
):
    """End-to-end: a dish the page presents as orderable must submit 200 and
    persist. On unfixed code the displayed date belongs to the outgoing week,
    so POST /orders returns 400 "Cannot order for a past date"."""
    body = logged_in_client.get("/modes/weekly").text
    week = extract_js_const(body, "WEEK")

    orderable_days = [day for day in week if day["orderable"] and day["items"]]
    print(
        f"\nPINNED {pinned_weekend}"
        f"\n  rendered dates  -> {[d['date'] for d in week]}"
        f"\n  orderable+items -> {[d['date'] for d in orderable_days]}"
    )
    assert orderable_days, (
        "the page rendered no day that is both stocked and orderable; "
        f"dates={[d['date'] for d in week]} "
        f"orderable={[d['orderable'] for d in week]} "
        f"items={[len(d['items']) for d in week]}"
    )

    day = orderable_days[0]
    resp = logged_in_client.post(
        "/orders",
        json={
            "order_date": day["date"],
            "items": [{"item_name": day["items"][0]["item_name"], "quantity": 1, "note": ""}],
        },
    )
    assert resp.status_code == 200, (
        f"a dish the page showed as orderable for {day['date']} was rejected: "
        f"{resp.status_code} {resp.text}"
    )

    rows = db.query(Order).filter(Order.user_id == user.id).all()
    assert [row.order_date.isoformat() for row in rows] == [day["date"]]
