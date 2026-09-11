"""Coverage for Mode 1's end-of-week "order for someone else?" loop (t8).

The operator's requirement, verbatim: "at the end when you are done with the
week, you get asked if you wanna order for someone else and who it is and it
runs the same prompt".

The loop itself is vanilla JS, so -- mirroring tests/test_weekly_interaction.py
-- these tests assert on the two things a route test legitimately can:

1. the *markup contract* the script builds (the yes/no question, the user
   picker and its accessible names, the #weekly-done-extra host, the
   startPromptFor() re-entry), so a rename cannot silently unhook the loop; and
2. the *server-side persistence contract* the loop drives -- a real
   POST /orders carrying `on_behalf_of`, read back through
   GET /orders/week/{username}, which is exactly what the browser performs.

The full click-through is additionally verified in a real browser; see the
task's end-to-end evidence.
"""


def rendered(logged_in_client):
    response = logged_in_client.get("/modes/weekly")
    assert response.status_code == 200
    return response.text


class TestAnotherPersonMarkup:
    def test_question_is_appended_into_the_done_extra_host(
        self, logged_in_client, menu_week
    ):
        """t6 left #weekly-done-extra empty for this question."""
        body = rendered(logged_in_client)

        assert 'id="weekly-done-extra"' in body
        assert "renderAnotherQuestion" in body

    def test_question_is_bound_to_the_weekly_done_event(self, logged_in_client, menu_week):
        """finish() dispatches weekly:done; the question hangs off that."""
        body = rendered(logged_in_client)

        assert 'document.addEventListener("weekly:done", renderAnotherQuestion)' in body

    def test_question_is_asked_in_czech(self, logged_in_client, menu_week):
        """UI copy is Czech."""
        body = rendered(logged_in_client)

        assert "Chcete objednat pro někoho dalšího?" in body
        assert "Pro koho objednáváme?" in body

    def test_yes_no_are_real_radio_controls(self, logged_in_client, menu_week):
        """Must be focusable controls with accessible names, not clickable
        divs -- wave 4 asserts on the ARIA tree."""
        body = rendered(logged_in_client)

        assert 'input.type = "radio"' in body
        assert 'input.name = "weekly-another"' in body
        assert '"weekly-another-yes"' in body
        assert '"weekly-another-no"' in body
        assert 'label.textContent = choice.label' in body

    def test_user_picker_is_a_real_select_with_a_label(self, logged_in_client, menu_week):
        body = rendered(logged_in_client)

        assert 'createElement("select")' in body
        assert 'select.id = "weekly-another-user"' in body
        assert 'selectLabel.htmlFor = "weekly-another-user"' in body

    def test_picker_is_sourced_from_the_existing_users_endpoint(
        self, logged_in_client, menu_week
    ):
        """EXISTING REGISTERED USERS ONLY -- no free-text names, no user
        creation, no schema change."""
        body = rendered(logged_in_client)

        assert 'fetch("/users"' in body

    def test_choosing_yes_re_runs_the_same_prompt_via_the_seam(
        self, logged_in_client, menu_week
    ):
        """The loop must reuse t6's stepper, not reimplement it."""
        body = rendered(logged_in_client)

        assert "startPromptFor(chosen)" in body
        assert "window.startPromptFor = startPromptFor" in body

    def test_loop_does_not_offer_the_person_just_ordered_for(
        self, logged_in_client, menu_week
    ):
        body = rendered(logged_in_client)

        assert "names.filter(name => name !== targetUser)" in body

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


class TestSecondPassPersistence:
    """The exact request sequence the loop performs on its second pass."""

    def test_second_pass_persists_under_the_target_user_id(
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

    def test_second_pass_leaves_the_originating_users_orders_untouched(
        self, logged_in_client, menu_week, other_user, user, next_weekday
    ):
        """The headline guarantee of the loop: pass two writes under the
        other_user's user id, so the first user's rows survive unchanged."""
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

    def test_loop_is_repeatable_person_after_person(
        self, logged_in_client, menu_week, other_user, admin_user, next_weekday
    ):
        """"person after person" -- each pass lands under its own user id."""
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
