"""Coverage for Mode 1's guided weekly prompt route (GET /modes/weekly).

This task owns the route, the template skeleton and the serialized payload the
client script steps through; the step-by-step interaction is built on top of it
separately. These tests therefore pin the *data contract* rather than the
interaction: if the payload's shape changes, the prompt breaks silently, so the
keys are asserted explicitly rather than just checking for a 200.

The payload is embedded in the page as `const WEEK = {...}` via Jinja's
`tojson`, matching how the order grid hands MENU to its own script. The tests
parse that constant back out of the HTML so they assert on what the browser
actually receives, not on an internal Python structure.
"""

import json
import re
from datetime import timedelta

from app.routers.pages import DAY_LABELS_CZ
from app.timezone import today_local, week_start


def extract_js_const(body: str, name: str):
    """Pull a `const NAME = <json>;` value back out of the rendered page."""
    match = re.search(rf"const {name} = (.*?);\n", body)
    assert match is not None, f"{name} not found in rendered page"
    return json.loads(match.group(1))


class TestWeeklyPromptAccess:
    def test_renders_for_logged_in_user(self, logged_in_client, menu_week):
        response = logged_in_client.get("/modes/weekly")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_redirects_anonymous_to_login(self, client):
        response = client.get("/modes/weekly")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestWeeklyPayloadShape:
    def test_groups_items_by_weekday_using_czech_labels(self, logged_in_client, menu_week):
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")

        labels = [day["label"] for day in week]
        assert labels == list(DAY_LABELS_CZ.values())

    def test_every_weekday_present_even_though_seeded_week_is_full(
        self, logged_in_client, menu_week
    ):
        """The contract promises all five weekdays as keys so the client never
        has to guard for a missing day."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")

        assert len(week) == 5
        assert all(day["items"] for day in week)

    def test_preserves_per_item_fields(self, logged_in_client, menu_week):
        """name/price/kcal/description must survive serialization -- the prompt
        renders all four."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")

        monday = week[0]
        soup = next(item for item in monday["items"] if item["name"] == "Monday soup")
        assert soup["price"] == 35
        assert soup["kcal"] == 120
        assert soup["desc"] == "test soup"
        assert soup["cat"] == "Polevka"

    def test_items_carry_the_same_keys_as_the_order_grid(self, logged_in_client, menu_week):
        """Shared helper with order_page -- the key set must not drift, or
        app.html's MENU consumers break."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")

        assert set(week[0]["items"][0]) == {"cat", "name", "desc", "price", "kcal"}

    def test_each_day_carries_its_iso_date(self, logged_in_client, menu_week):
        """Dates are computed server-side so the client can post order_date
        verbatim instead of re-deriving it in JS."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")
        ws = week_start(today_local())

        assert [day["date"] for day in week] == [
            (ws + timedelta(days=offset)).isoformat() for offset in range(5)
        ]

    def test_past_days_are_marked_not_orderable(self, logged_in_client, menu_week):
        """Mirrors _check_ordering_allowed so the client can mark or skip days
        instead of discovering a 400 on submit."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")
        today = today_local()

        for day in week:
            expected = day["date"] >= today.isoformat()
            assert day["orderable"] is expected


class TestWeeklyEmptyState:
    def test_week_with_no_menu_renders_empty_state_not_500(self, logged_in_client):
        """No `menu_week` fixture -- nothing is loaded for this week."""
        response = logged_in_client.get("/modes/weekly")

        assert response.status_code == 200
        assert "není načtené žádné menu" in response.text

    def test_empty_week_still_exposes_all_five_days(self, logged_in_client):
        """The payload stays iterable with an empty menu rather than collapsing
        to an empty list."""
        week = extract_js_const(logged_in_client.get("/modes/weekly").text, "WEEK")

        assert [day["label"] for day in week] == list(DAY_LABELS_CZ.values())
        assert all(day["items"] == [] for day in week)

    def test_has_menu_flag_reflects_a_loaded_week(self, logged_in_client, menu_week):
        body = logged_in_client.get("/modes/weekly").text

        assert extract_js_const(body, "HAS_MENU") is True
        assert "není načtené žádné menu" not in body
