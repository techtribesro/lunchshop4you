"""Coverage for Mode 1's "Pro koho objednáváte?" target selector (t13).

The operator's requirement, verbatim: "change the weekly prompt to who are you
ordering for and default is 'me' and then a drop menu for others thats' it why
overcomplicate".

This REPLACED t8's end-of-week "order for someone else?" question, which was
only offered AFTER the whole week had already been submitted. The selector is
answered UP FRONT instead, so the target is visible by construction -- no hint
text, banner or trailing question is layered on top.

The interaction itself is vanilla JS, so -- mirroring
tests/test_weekly_interaction.py -- these tests assert on the two things a
route test legitimately can:

1. the *markup contract* the page ships (a real <select> with a real <label>,
   defaulting to the logged-in user, sourced from GET /users, re-entering the
   startPromptFor() seam), so a rename cannot silently unhook the selector; and
2. the *server-side persistence contract* the selector drives -- a real
   POST /orders carrying `on_behalf_of`, read back through
   GET /orders/week/{username}, which is exactly what the browser performs.

The full click-through is additionally verified in a real browser; see the
task's end-to-end evidence.
"""


def rendered(logged_in_client):
    response = logged_in_client.get("/modes/weekly")
    assert response.status_code == 200
    return response.text


class TestTargetSelectorMarkup:
    def test_selector_is_server_rendered_not_injected_after_submission(
        self, logged_in_client, menu_week
    ):
        """The whole point of t13: the question is present on arrival, before
        any picking. The old flow injected it into #weekly-done-extra from
        finish(); that host and its question must be gone."""
        body = rendered(logged_in_client)

        assert 'id="weekly-target-user"' in body
        assert 'id="weekly-done-extra"' not in body
        assert "renderAnotherQuestion" not in body

    def test_end_of_week_question_is_gone(self, logged_in_client, menu_week):
        """Leaving BOTH the old question and the new selector in place would be
        exactly the overcomplication the operator rejected."""
        body = rendered(logged_in_client)

        assert "Chcete objednat pro někoho dalšího?" not in body
        assert "weekly:done" not in body
        assert "weekly-another" not in body

    def test_question_is_asked_in_czech(self, logged_in_client, menu_week):
        """UI copy is Czech."""
        body = rendered(logged_in_client)

        assert "Pro koho objednáváte?" in body

    def test_selector_is_a_real_select_with_a_real_label(
        self, logged_in_client, menu_week
    ):
        """Must be a focusable control with an accessible name, not a clickable
        div -- wave 4 asserts on the ARIA tree."""
        body = rendered(logged_in_client)

        assert '<select id="weekly-target-user"' in body
        assert 'for="weekly-target-user"' in body

    def test_selector_defaults_to_the_logged_in_user(
        self, logged_in_client, user, menu_week
    ):
        """"default is 'me'" -- rendered server-side as the selected option so
        it is correct on arrival even before GET /users resolves."""
        body = rendered(logged_in_client)

        assert f'<option value="{user.username}" selected>{user.username}</option>' in body

    def test_selector_is_sourced_from_the_existing_users_endpoint(
        self, logged_in_client, menu_week
    ):
        """EXISTING REGISTERED USERS ONLY -- no free-text names, no user
        creation, no schema change."""
        body = rendered(logged_in_client)

        assert 'fetch("/users"' in body

    def test_selector_does_not_offer_the_logged_in_user_twice(
        self, logged_in_client, menu_week
    ):
        """"me" is already the server-rendered default; the script appends
        only everyone else."""
        body = rendered(logged_in_client)

        assert "filter(name => name !== USERNAME)" in body

    def test_changing_the_selector_re_runs_the_same_prompt_via_the_seam(
        self, logged_in_client, menu_week
    ):
        """The selector must reuse the existing stepper, not reimplement it."""
        body = rendered(logged_in_client)

        assert "startPromptFor(targetSelect.value)" in body
        assert "window.startPromptFor = startPromptFor" in body

    def test_no_overwrite_guard_is_introduced(self, logged_in_client, menu_week):
        """goal.md ACCEPTED RISK: ordering for a colleague who already ordered
        that day overwrites it. The operator refused guards; adding a confirm
        dialog here would be scope they declined."""
        body = rendered(logged_in_client)

        assert "confirm(" not in body


class TestUserPickerSource:
    def test_users_endpoint_lists_existing_users_for_any_logged_in_user(
        self, logged_in_client, other_user
    ):
        resp = logged_in_client.get("/users")

        assert resp.status_code == 200
        assert other_user.username in resp.json()


class TestOnBehalfPersistence:
    """The exact request sequence the selector performs for a colleague."""

    def test_ordering_for_a_colleague_persists_under_the_target_user_id(
        self, logged_in_client, menu_week, other_user, next_weekday
    ):
        day_name = next_weekday.strftime("%A")

        resp = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "on_behalf_of": other_user.username,
                "items": [
                    {"item_name": f"{day_name} main 1", "quantity": 1, "note": "bez cibule"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        week = logged_in_client.get(f"/orders/week/{other_user.username}").json()
        line = next(row for row in week if row["order_date"] == next_weekday.isoformat())
        assert line["item_name"] == f"{day_name} main 1"
        assert line["note"] == "bez cibule"

    def test_ordering_for_a_colleague_leaves_my_own_orders_untouched(
        self, logged_in_client, menu_week, other_user, user, next_weekday
    ):
        """The headline guarantee: a colleague's pass writes under the
        colleague's user id, so my rows survive unchanged."""
        day_name = next_weekday.strftime("%A")

        first = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "items": [
                    {"item_name": f"{day_name} soup", "quantity": 1, "note": "moje"}
                ],
            },
        )
        assert first.status_code == 200, first.text

        second = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "on_behalf_of": other_user.username,
                "items": [
                    {"item_name": f"{day_name} main 2", "quantity": 1, "note": "kolega"}
                ],
            },
        )
        assert second.status_code == 200, second.text

        mine = logged_in_client.get(f"/orders/week/{user.username}").json()
        my_lines = [row for row in mine if row["order_date"] == next_weekday.isoformat()]
        assert len(my_lines) == 1
        assert my_lines[0]["item_name"] == f"{day_name} soup"
        assert my_lines[0]["note"] == "moje"

        theirs = logged_in_client.get(f"/orders/week/{other_user.username}").json()
        their_lines = [row for row in theirs if row["order_date"] == next_weekday.isoformat()]
        assert len(their_lines) == 1
        assert their_lines[0]["item_name"] == f"{day_name} main 2"

    def test_selector_is_repeatable_person_after_person(
        self, logged_in_client, menu_week, other_user, admin_user, next_weekday
    ):
        """Switching the selector again lands under the next person's id."""
        day_name = next_weekday.strftime("%A")

        for target, item in (
            (other_user.username, f"{day_name} main 1"),
            (admin_user.username, f"{day_name} main 2"),
        ):
            resp = logged_in_client.post(
                "/orders",
                json={
                    "order_date": next_weekday.isoformat(),
                    "on_behalf_of": target,
                    "items": [{"item_name": item, "quantity": 1, "note": ""}],
                },
            )
            assert resp.status_code == 200, resp.text

        for target, item in (
            (other_user.username, f"{day_name} main 1"),
            (admin_user.username, f"{day_name} main 2"),
        ):
            week = logged_in_client.get(f"/orders/week/{target}").json()
            line = next(row for row in week if row["order_date"] == next_weekday.isoformat())
            assert line["item_name"] == item
