"""Coverage for Mode 1's guided stepping interaction (the client layer).

The route and its payload are pinned by tests/test_weekly_prompt.py. This file
covers what is layered on top: the step host markup, the lettered-choice
controls, the note field, and the persistence contract the script posts
through.

The interaction itself is vanilla JS, so these tests assert on two things a
route test legitimately can:

1. the *markup contract* the script binds to (element ids, the note field's
   255-char cap, the presence of a step host and an end-of-week section), so a
   rename cannot silently unhook the flow; and
2. the *server-side persistence contract* the script drives -- a real
   POST /orders per day followed by GET /orders/my-week, which is exactly the
   sequence the browser performs.

The full click-through is additionally verified in a real browser; see the
task's end-to-end evidence.
"""

import re

from app.timezone import today_local


def rendered(logged_in_client):
    response = logged_in_client.get("/modes/weekly")
    assert response.status_code == 200
    return response.text


class TestStepHostMarkup:
    def test_page_exposes_a_step_host_and_done_section(self, logged_in_client, menu_week):
        """The script fills #weekly-step and reveals #weekly-done at the end."""
        body = rendered(logged_in_client)

        assert 'id="weekly-step"' in body
        assert 'id="weekly-done"' in body

    def test_step_host_starts_hidden_so_it_cannot_flash_before_scripting(
        self, logged_in_client, menu_week
    ):
        body = rendered(logged_in_client)

        step = re.search(r'<section id="weekly-step"[^>]*>', body).group(0)
        assert "hidden" in step

    def test_note_input_is_capped_at_the_server_truncation_limit(
        self, logged_in_client, menu_week
    ):
        """Server stores `line.note.strip()[:255]`; the UI must not accept more
        than it will keep."""
        body = rendered(logged_in_client)

        note = re.search(r'<input type="text" id="weekly-note".*?/>', body, re.S).group(0)
        assert 'maxlength="255"' in note

    def test_note_field_has_an_associated_label(self, logged_in_client, menu_week):
        """Accessible name for the note box -- wave 4 asserts on the ARIA tree."""
        body = rendered(logged_in_client)

        assert 'for="weekly-note"' in body

    def test_step_controls_are_real_buttons(self, logged_in_client, menu_week):
        """The lettered options and the navigation must be focusable controls,
        not clickable divs."""
        body = rendered(logged_in_client)

        for control_id in ("weekly-back", "weekly-skip", "weekly-next"):
            assert re.search(rf'<button[^>]*id="{control_id}"', body), control_id

    def test_choices_are_rendered_as_radio_inputs(self, logged_in_client, menu_week):
        """The script builds `input[type=radio][name=weekly-choice]` per item."""
        body = rendered(logged_in_client)

        assert 'input.type = "radio"' in body
        assert 'input.name = "weekly-choice"' in body

    def test_lettered_options_use_an_a_b_c_prefix(self, logged_in_client, menu_week):
        """Mode 1 is specified as a 'simple abc prompt'."""
        body = rendered(logged_in_client)

        assert 'const LETTERS = "abcdefghijklmnopqrstuvwxyz"' in body

    def test_empty_week_renders_no_step_host(self, logged_in_client):
        """Nothing to step through when no menu is loaded -- empty state only."""
        body = rendered(logged_in_client)

        assert 'id="weekly-step"' not in body
        assert "není načtené žádné menu" in body

    def test_leaves_a_named_seam_for_the_on_behalf_loop(self, logged_in_client, menu_week):
        """t13 replaced the end-of-week question (and its #weekly-done-extra
        host) with the up-front "Pro koho objednáváte?" selector, which
        re-enters the very same startPromptFor() seam. Both must stay
        addressable."""
        body = rendered(logged_in_client)

        assert 'id="weekly-target-user"' in body
        assert "window.startPromptFor = startPromptFor" in body

    def test_posts_the_server_computed_date_verbatim(self, logged_in_client, menu_week):
        """Re-deriving the date in JS reintroduces the toISOString() UTC
        off-by-one bug app.html carries a scar comment about."""
        body = rendered(logged_in_client)

        assert "order_date: day.date" in body


class TestGuidedFlowPersistence:
    """The exact request sequence the stepping script performs."""

    def test_a_day_choice_with_a_note_round_trips(
        self, logged_in_client, menu_week, next_weekday
    ):
        day_name = next_weekday.strftime("%A")
        resp = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "items": [
                    {"item_name": f"{day_name} main 1", "quantity": 1, "note": "bez cibule"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        week = logged_in_client.get("/orders/my-week").json()
        line = next(row for row in week if row["order_date"] == next_weekday.isoformat())
        assert line["item_name"] == f"{day_name} main 1"
        assert line["note"] == "bez cibule"

    def test_note_is_truncated_to_255_characters(
        self, logged_in_client, menu_week, next_weekday
    ):
        """`Order.note` is String(255) and the route applies
        `.strip()[:255]` -- a longer note must not error, just truncate."""
        day_name = next_weekday.strftime("%A")
        long_note = "x" * 400

        resp = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "items": [
                    {"item_name": f"{day_name} soup", "quantity": 1, "note": long_note}
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        week = logged_in_client.get("/orders/my-week").json()
        line = next(row for row in week if row["order_date"] == next_weekday.isoformat())
        assert len(line["note"]) == 255
        assert line["note"] == "x" * 255

    def test_a_past_day_is_rejected_so_the_ui_must_not_offer_it(
        self, logged_in_client, menu_week
    ):
        """Justifies honouring `orderable`: submitting a past day 400s, so the
        step host skips those days rather than letting the user hit an error."""
        today = today_local()
        if today.weekday() == 0:
            # Monday: no earlier weekday in this menu week to submit against.
            return

        monday = today.fromordinal(today.toordinal() - today.weekday())
        resp = logged_in_client.post(
            "/orders",
            json={
                "order_date": monday.isoformat(),
                "items": [{"item_name": "Monday soup", "quantity": 1, "note": ""}],
            },
        )
        assert resp.status_code == 400
