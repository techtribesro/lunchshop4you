"""Regression sweep over everything this run did NOT set out to change.

Four waves added a mode chooser (t3), a guided weekly prompt (t5/t6/t11),
on-behalf ordering (t4/t8) and a full re-theme (t9/t10). All of them edited
`app/templates/app.html` -- 945 lines, touched by four tasks across three
waves -- and `app/routers/pages.py`. The risk this module exists to catch is
that something PRE-EXISTING quietly broke while those landed.

Scope, matching the acceptance criterion "no regression in the existing order
grid, dashboard, admin or Sheets sync":

  * the oldschool order grid still adds/removes items and totals correctly
  * the Přehled dashboard still renders rows and aggregates
  * the admin tab still lists users and Telegram subscribers
  * the admin send-out modal still gates on typing ODESLAT
  * sheets sync is still invoked on order write (mocked, never a live call)

TWO DELIBERATE CHOICES ABOUT WHAT IS ASSERTED HERE

1. Server-side behaviour is asserted through real HTTP requests; the browser
   half of this task's verify covers the rendered/interactive side. Where the
   behaviour lives ONLY in inline vanilla JS (the grid's totalling, the
   ODESLAT gate), these tests assert the shipped template text, following the
   precedent set by tests/test_orders_on_behalf_ui.py: the page is
   server-rendered Jinja2 with no build step, so the template IS the shipped
   artefact and there is no JS module to import. The browser pass is what
   proves those strings actually behave; these assertions are the regression
   tripwire that they have not been deleted or renamed.

2. `sync_all` is asserted to be CALLED, never to succeed. It is a best-effort
   reporting mirror (app.routers.admin._sync_all_best_effort swallows
   exceptions by design) and it reaches for Google credentials, so the
   `stub_sheets_sync` fixture records calls instead of letting any leave the
   machine.

Fixtures live in this module rather than in tests/conftest.py, which is owned
by another task this wave.
"""

from datetime import timedelta

import pytest

from app.models import Order, TelegramSubscriber
from app.timezone import today_local, week_start
from tests.conftest import _next_orderable_weekday


@pytest.fixture
def orderable_weekdays() -> list:
    """Every weekday of the orderable menu week that is actually orderable.

    `_check_ordering_allowed` rejects past dates and weekends, so which days
    qualify depends on what day the suite runs. On a Friday this list has a
    single entry -- which is exactly the trap that made an earlier end-to-end
    run in this project look green while only ever exercising one day. Tests
    that need more than one orderable day skip explicitly (and say so) rather
    than silently degrading to a single-day check.

    ANCHORED ON conftest's `_next_orderable_weekday`, deliberately, and NOT on
    `today_local()` directly. This fixture previously filtered the CURRENT
    week for days >= today. On a Saturday or Sunday every weekday of the
    current week is already in the past, so the list came back EMPTY and ten
    tests died on `orderable_weekdays[0]` with IndexError -- the suite was red
    purely because the calendar rolled over. Re-deriving "orderable" here was
    the root cause: conftest already owns that definition (today if today is a
    weekday, else the coming Monday) and `menu_week` seeds exactly the week it
    lands in, with the same per-weekday item names `_item_for` reconstructs.
    Two fixtures disagreeing about which days are orderable is what broke;
    reusing the one definition is what fixes it.

    On a weekday the anchor IS today, so this yields precisely what it always
    did (Fri -> one day, keeping the skip in
    `test_multiple_orderable_days_are_independent` meaningful). On a weekend
    the anchor is the coming Monday and all five days of that seeded week are
    orderable, so the sweep exercises real journeys instead of opting out.
    """
    anchor = _next_orderable_weekday()
    ws = week_start(anchor)
    return [ws + timedelta(days=i) for i in range(5) if ws + timedelta(days=i) >= anchor]


@pytest.fixture
def dashboard_order_date():
    """A weekday of the CURRENT week that `compute_dashboard` actually counts.

    Deliberately a different date from `orderable_weekdays`, because the two
    routes have genuinely different, pre-existing date rules and on a weekend
    NO single date satisfies both:

      * POST /orders -> `_check_ordering_allowed` rejects past dates, so it
        needs a date >= today (on Sat/Sun that is next week's Monday).
      * GET /dashboard -> `compute_dashboard` filters
        `order_date <= today_local()` and week-aggregates only rows whose
        `week_start == week_start()` (the CURRENT week), so it can only ever
        see a current-week day that is NOT in the future.

    Overlap of those two sets is `[today]` on a weekday and EMPTY on Sat/Sun.
    So the dashboard tests anchor here instead, and seat their rows through
    POST /admin/orders -- `assign_order` rejects weekends but deliberately
    NOT past dates ("Still only allows weekdays in an already-loaded week"),
    which is a real shipped code path, not a test-only backdoor. That keeps
    the dashboard assertions exercising real aggregation on every weekday
    instead of being skipped or weakened on a weekend.

    Returns the latest current-week weekday that is <= today: today itself on
    a weekday, Friday of the just-finished week on Sat/Sun. `menu_week` seeds
    the current week, so `_item_for` always resolves.
    """
    today = today_local()
    ws = week_start(today)
    candidates = [ws + timedelta(days=i) for i in range(5) if ws + timedelta(days=i) <= today]
    assert candidates, f"no current-week weekday on or before {today}"
    return candidates[-1]


def _item_for(order_date, suffix: str = "main 1") -> str:
    """Menu item name seeded by the `menu_week` fixture for a given date."""
    return f"{order_date.strftime('%A')} {suffix}"


class TestOldschoolOrderGridServerContract:
    """The grid's add/remove/total behaviour, through the POST /orders contract
    the grid actually drives."""

    def test_adding_items_persists_them_with_correct_line_prices(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync, db
    ):
        order_date = orderable_weekdays[0]
        soup = _item_for(order_date, "soup")
        main = _item_for(order_date, "main 1")

        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [
                    {"item_name": soup, "quantity": 2, "note": ""},
                    {"item_name": main, "quantity": 1, "note": "bez cibule"},
                ],
            },
        )
        assert response.status_code == 200, response.text

        stored = {o.item_name: o for o in db.query(Order).filter(Order.order_date == order_date).all()}
        assert set(stored) == {soup, main}
        assert stored[soup].quantity == 2
        assert stored[main].note == "bez cibule"
        # Unit price comes from the menu, not the client -- a client-supplied
        # price would be a way to order a 300 Kč main for 1 Kč.
        assert stored[soup].unit_price_czk == 35
        assert stored[main].unit_price_czk == 136

    def test_removing_an_item_is_a_resubmit_without_it(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync, db
    ):
        """The grid removes a line by dropping it from the cart and resubmitting
        the whole day (changeQty deletes the key at qty 0). POST /orders is
        delete-then-reinsert, so the dropped row must actually disappear."""
        order_date = orderable_weekdays[0]
        soup = _item_for(order_date, "soup")
        main = _item_for(order_date, "main 1")

        logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [
                    {"item_name": soup, "quantity": 1, "note": ""},
                    {"item_name": main, "quantity": 1, "note": ""},
                ],
            },
        )
        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [{"item_name": main, "quantity": 1, "note": ""}],
            },
        )
        assert response.status_code == 200, response.text

        remaining = [o.item_name for o in db.query(Order).filter(Order.order_date == order_date).all()]
        assert remaining == [main], "the removed line should be gone, not orphaned"

    def test_submitting_an_empty_cart_clears_the_day(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync, db
    ):
        order_date = orderable_weekdays[0]
        logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 1, "note": ""}],
            },
        )
        response = logged_in_client.post(
            "/orders", json={"order_date": order_date.isoformat(), "items": []}
        )
        assert response.status_code == 200, response.text
        assert db.query(Order).filter(Order.order_date == order_date).count() == 0

    def test_order_total_matches_sum_of_quantity_times_unit_price(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync
    ):
        """The grid's summary total is `sum(price * qty)` (renderSummary). The
        persisted rows must support that same arithmetic."""
        order_date = orderable_weekdays[0]
        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [
                    {"item_name": _item_for(order_date, "soup"), "quantity": 2, "note": ""},
                    {"item_name": _item_for(order_date, "main 2"), "quantity": 1, "note": ""},
                ],
            },
        )
        assert response.status_code == 200, response.text

        lines = response.json()
        total = sum(line["quantity"] * line["unit_price_czk"] for line in lines)
        assert total == 2 * 35 + 137

    def test_ordering_rules_still_reject_past_dates_and_weekends(
        self, logged_in_client, menu_week, stub_sheets_sync
    ):
        """_check_ordering_allowed is pre-existing behaviour none of this run's
        work was meant to touch."""
        today = today_local()
        past = today - timedelta(days=7)
        saturday = week_start(today) + timedelta(days=5)

        past_response = logged_in_client.post(
            "/orders", json={"order_date": past.isoformat(), "items": []}
        )
        assert past_response.status_code == 400

        weekend_response = logged_in_client.post(
            "/orders", json={"order_date": saturday.isoformat(), "items": []}
        )
        assert weekend_response.status_code == 400

    def test_multiple_orderable_days_are_independent(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync, db
    ):
        """Submitting one day must not disturb another. This needs TWO orderable
        days; on a Friday there is only one, so skip loudly rather than pass
        while exercising a single day."""
        if len(orderable_weekdays) < 2:
            pytest.skip(
                f"needs 2+ orderable weekdays, today ({today_local()}) leaves "
                f"{len(orderable_weekdays)}"
            )
        first, second = orderable_weekdays[0], orderable_weekdays[1]

        for order_date in (first, second):
            response = logged_in_client.post(
                "/orders",
                json={
                    "order_date": order_date.isoformat(),
                    "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 1, "note": ""}],
                },
            )
            assert response.status_code == 200, response.text

        assert db.query(Order).filter(Order.order_date == first).count() == 1
        assert db.query(Order).filter(Order.order_date == second).count() == 1

        # Re-submitting the first day must leave the second alone.
        logged_in_client.post(
            "/orders", json={"order_date": first.isoformat(), "items": []}
        )
        assert db.query(Order).filter(Order.order_date == first).count() == 0
        assert db.query(Order).filter(Order.order_date == second).count() == 1


class TestOrderGridMarkupSurvived:
    """The grid controls the browser pass drives. String assertions against the
    served template -- see the module docstring for why."""

    def test_grid_controls_are_present(self, logged_in_client, menu_week):
        html = logged_in_client.get("/orders").text
        for marker in ('id="menu-list"', 'id="day-row"', 'id="summary-body"',
                       'id="summary-total"', 'id="submit-order"'):
            assert marker in html, f"missing grid control {marker}"

    def test_quantity_steppers_and_totalling_logic_survived(self, logged_in_client, menu_week):
        html = logged_in_client.get("/orders").text
        assert 'data-action="inc"' in html
        assert 'data-action="dec"' in html
        # The actual totalling expression the summary depends on.
        assert "sum + e.price * e.qty" in html

    def test_menu_items_are_rendered_into_the_page(self, logged_in_client, menu_week):
        html = logged_in_client.get("/orders").text
        assert "Monday soup" in html
        assert "Monday main 1" in html


class TestDashboardStillAggregates:
    """GET /dashboard -- the data behind the Přehled tab."""

    def test_dashboard_returns_rows_and_aggregates_for_an_order(
        self, logged_in_client, admin_client, user, menu_week, dashboard_order_date,
        stub_sheets_sync
    ):
        order_date = dashboard_order_date
        admin_client.post(
            "/admin/orders",
            json={
                "username": user.username,
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 2, "note": ""}],
            },
        )

        response = logged_in_client.get("/dashboard")
        assert response.status_code == 200, response.text
        data = response.json()

        rows = [r for r in data["rows"] if r["user"] == user.username]
        assert rows, "the ordering user should appear in the dashboard rows"
        row = rows[0]
        assert row["order_date"] == order_date.isoformat()
        assert row["items_ordered"] == 2
        assert row["daily_total_czk"] == 70
        assert row["daily_total_kcal"] == 240  # 120 kcal * 2

        assert data["week_aggregate_czk"] >= 70
        assert data["month_aggregate_czk"] >= 70
        assert data["week_aggregate_kcal"] >= 240

    def test_dashboard_aggregates_across_two_users(
        self, logged_in_client, admin_client, client, user, other_user, menu_week,
        dashboard_order_date, stub_sheets_sync
    ):
        """The aggregate is everyone's total, not just the caller's."""
        from tests.conftest import USER_PASSWORD, login_as

        order_date = dashboard_order_date
        admin_client.post(
            "/admin/orders",
            json={
                "username": user.username,
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 1, "note": ""}],
            },
        )
        # Second user's order, seated the same way so it lands in the window
        # `compute_dashboard` aggregates over.
        admin_client.post(
            "/admin/orders",
            json={
                "username": other_user.username,
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "main 1"), "quantity": 1, "note": ""}],
            },
        )
        login_as(client, other_user.username, USER_PASSWORD)

        data = client.get("/dashboard").json()
        users_with_rows = {r["user"] for r in data["rows"]}
        assert {user.username, other_user.username} <= users_with_rows
        assert data["week_aggregate_czk"] >= 35 + 136

    def test_dashboard_is_empty_but_well_formed_with_no_orders(self, logged_in_client):
        data = logged_in_client.get("/dashboard").json()
        assert data["rows"] == []
        assert data["week_aggregate_czk"] == 0
        assert data["month_aggregate_czk"] == 0

    def test_dashboard_requires_a_session(self, client):
        assert client.get("/dashboard").status_code == 401

    def test_dashboard_tab_and_table_markup_survived(self, logged_in_client):
        html = logged_in_client.get("/orders").text
        assert 'data-goto="dashboard"' in html
        assert "Přehled" in html
        for marker in ('id="dash-body"', 'id="dash-foot-day"', 'id="dash-foot-week"',
                       'id="dash-foot-month"', 'id="stat-week-all"'):
            assert marker in html, f"missing dashboard element {marker}"


class TestAdminTabStillWorks:
    """The admin tab's two lists, and the routes that populate them."""

    def test_admin_users_endpoint_lists_users(self, admin_client, admin_user, user, other_user):
        response = admin_client.get("/admin/users")
        assert response.status_code == 200, response.text

        usernames = [u["username"] for u in response.json()]
        assert {admin_user.username, user.username, other_user.username} <= set(usernames)
        assert usernames == sorted(usernames), "list_users orders by username"

        admin_row = next(u for u in response.json() if u["username"] == admin_user.username)
        assert admin_row["is_admin"] is True
        assert "created_at" in admin_row  # the JS formats this column

    def test_telegram_subscribers_endpoint_lists_subscribers(self, admin_client, db):
        db.add_all([
            TelegramSubscriber(chat_id="111", display_name="Alice"),
            TelegramSubscriber(chat_id="222", display_name="Bob"),
        ])
        db.commit()

        response = admin_client.get("/admin/telegram-subscribers")
        assert response.status_code == 200, response.text

        subs = response.json()
        assert {s["display_name"] for s in subs} == {"Alice", "Bob"}
        assert {s["chat_id"] for s in subs} == {"111", "222"}
        assert all("subscribed_at" in s for s in subs)

    def test_telegram_subscribers_empty_list_is_valid(self, admin_client):
        response = admin_client.get("/admin/telegram-subscribers")
        assert response.status_code == 200
        assert response.json() == []

    def test_admin_routes_still_reject_non_admins(self, logged_in_client):
        assert logged_in_client.get("/admin/users").status_code == 403
        assert logged_in_client.get("/admin/telegram-subscribers").status_code == 403

    def test_admin_tab_is_hidden_from_non_admins(self, logged_in_client):
        """The tab and its panel are Jinja-gated on user.is_admin."""
        html = logged_in_client.get("/orders").text
        assert 'data-goto="admin"' not in html
        assert 'id="admin-users-body"' not in html

    def test_admin_tab_markup_is_present_for_admins(self, admin_client):
        html = admin_client.get("/orders").text
        assert 'data-goto="admin"' in html
        assert 'id="admin-users-body"' in html
        assert 'id="telegram-subscribers-body"' in html
        # Both lists are refreshed when the tab is opened.
        assert "refreshAdminUsers()" in html
        assert "refreshTelegramSubscribers()" in html


class TestSendOutModalGate:
    """The destructive send-out action is behind a typed confirmation."""

    def test_modal_markup_and_confirm_phrase_survived(self, admin_client):
        html = admin_client.get("/orders").text
        assert 'id="topbar-send-out"' in html
        assert 'id="send-out-modal"' in html
        assert 'id="send-out-confirm-input"' in html
        assert 'id="send-out-confirm"' in html
        assert 'SEND_OUT_PHRASE = "ODESLAT"' in html

    def test_confirm_button_ships_disabled(self, admin_client):
        """The gate's whole point: the button is inert until the phrase matches,
        so its initial rendered state must be disabled."""
        html = admin_client.get("/orders").text
        send_out_button = html.split('id="send-out-confirm"')[1].split(">")[0]
        assert "disabled" in send_out_button

    def test_gate_compares_typed_value_against_the_phrase(self, admin_client):
        """The enabling condition itself -- typed value, trimmed and uppercased,
        must equal ODESLAT."""
        html = admin_client.get("/orders").text
        assert 'sendOutConfirmBtn.disabled = sendOutInput.value.trim().toUpperCase() !== SEND_OUT_PHRASE' in html

    def test_modal_is_not_rendered_for_non_admins(self, logged_in_client):
        html = logged_in_client.get("/orders").text
        assert 'id="send-out-modal"' not in html
        assert 'id="topbar-send-out"' not in html

    def test_send_summary_route_is_admin_only(self, logged_in_client):
        """Even if the modal were bypassed, the route stays gated."""
        assert logged_in_client.post("/admin/send-order-summary").status_code == 403


class TestSheetsSyncStillInvokedOnWrite:
    """Sheets sync is asserted to be CALLED, never to succeed -- see the module
    docstring. Every call here is the recording stub, so nothing reaches
    Google."""

    def test_sync_is_called_on_order_submit(
        self, logged_in_client, menu_week, orderable_weekdays, stub_sheets_sync
    ):
        order_date = orderable_weekdays[0]
        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 1, "note": ""}],
            },
        )
        assert response.status_code == 200, response.text
        assert len(stub_sheets_sync) == 1, "POST /orders must invoke sync_all exactly once"

    def test_sync_is_called_on_an_on_behalf_submit(
        self, logged_in_client, other_user, menu_week, orderable_weekdays, stub_sheets_sync
    ):
        """The on-behalf path added by this run must not have skipped the
        mirror."""
        order_date = orderable_weekdays[0]
        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "main 1"), "quantity": 1, "note": ""}],
                "on_behalf_of": other_user.username,
            },
        )
        assert response.status_code == 200, response.text
        assert len(stub_sheets_sync) == 1

    def test_sync_is_not_called_when_the_write_is_rejected(
        self, logged_in_client, menu_week, stub_sheets_sync
    ):
        """A 400 means nothing was written, so there is nothing to mirror."""
        saturday = week_start(today_local()) + timedelta(days=5)
        response = logged_in_client.post(
            "/orders", json={"order_date": saturday.isoformat(), "items": []}
        )
        assert response.status_code == 400
        assert stub_sheets_sync == []

    def test_sync_is_called_on_admin_order_assignment(
        self, admin_client, user, menu_week, orderable_weekdays, stub_sheets_sync
    ):
        order_date = orderable_weekdays[0]
        response = admin_client.post(
            "/admin/orders",
            json={
                "username": user.username,
                "order_date": order_date.isoformat(),
                "items": [{"item_name": _item_for(order_date, "soup"), "quantity": 1, "note": ""}],
            },
        )
        assert response.status_code == 200, response.text
        assert len(stub_sheets_sync) == 1

    def test_sync_is_called_on_admin_clear_orders(
        self, admin_client, menu_week, orderable_weekdays, stub_sheets_sync
    ):
        response = admin_client.request(
            "DELETE",
            "/admin/orders/by-date",
            params={"order_date": orderable_weekdays[0].isoformat()},
        )
        assert response.status_code == 200, response.text
        assert len(stub_sheets_sync) == 1

    def test_no_real_sheets_call_can_escape(self, stub_sheets_sync):
        """Guard on the guard: the stub really is bound at the order router's
        import site, so `sync_all` there is not the live Google client."""
        import app.routers.orders as orders_module

        assert orders_module.sync_all.__name__ == "_fake_sync_all"
