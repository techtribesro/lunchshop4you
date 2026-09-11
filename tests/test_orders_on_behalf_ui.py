"""Oldschool order screen: the distinct "order for someone else" button.

t4 made `on_behalf_of` on POST /orders available to any logged-in user; the
API itself is covered by tests/test_orders_on_behalf.py. What these tests pin
down is the UI half rendered by app/templates/app.html:

  * the dedicated button exists and is separate from the read-only
    "Objednávám za" pill row (#people-row), which must NOT become an
    ordering surface by itself;
  * the client submits on-behalf orders through POST /orders with
    `on_behalf_of` rather than the admin-only /admin/orders;
  * a non-admin really can persist rows under the target user, observable
    through GET /orders/week/{username} exactly as the browser check does.

These are string assertions against the served template. That is deliberate:
the page is server-rendered Jinja2 + inline vanilla JS with no build step, so
the template *is* the shipped artefact and there is no JS module to import.
"""

from tests.conftest import USER_PASSWORD, login_as


def _order_page(logged_in_client) -> str:
    response = logged_in_client.get("/orders")
    assert response.status_code == 200, response.text
    return response.text


def test_order_page_exposes_distinct_on_behalf_button(logged_in_client):
    """A non-admin sees a dedicated control to start an on-behalf order."""
    html = _order_page(logged_in_client)
    assert 'id="on-behalf-start"' in html
    assert "Objednat za n" in html  # "Objednat za někoho jiného", Czech copy


def test_on_behalf_controls_include_picker_and_exit_path(logged_in_client):
    """The flow is enterable (picker) and leavable (explicit exit control)."""
    html = _order_page(logged_in_client)
    assert 'id="on-behalf-select"' in html
    assert 'id="on-behalf-exit"' in html
    assert "Zp" in html and "moji objedn" in html  # "Zpět na moji objednávku"


def test_acting_as_state_is_visually_announced(logged_in_client):
    """The acting-as state must be unmistakable, not a silent mode switch."""
    html = _order_page(logged_in_client)
    assert 'id="on-behalf-banner"' in html
    assert 'role="status"' in html


def test_people_row_still_present_as_readonly_viewer(logged_in_client):
    """The pre-existing pill row is preserved, not repurposed or removed."""
    html = _order_page(logged_in_client)
    assert 'id="people-row"' in html
    # Selecting a pill drops out of ordering mode, so viewing stays read-only.
    assert "onBehalfMode = false" in html


def test_client_submits_on_behalf_via_orders_not_admin_orders(logged_in_client):
    """Non-admin on-behalf submits must use the t4 field on POST /orders."""
    html = _order_page(logged_in_client)
    assert "on_behalf_of: actingAsUser" in html


def test_non_admin_on_behalf_submit_persists_under_target_user(
    logged_in_client, user, other_user, menu_week, next_weekday, stub_sheets_sync
):
    """The backend contract the button drives, asserted the way the browser
    check confirms it: rows land under the TARGET user's week."""
    item_name = f"{next_weekday.strftime('%A')} main 1"

    response = logged_in_client.post(
        "/orders",
        json={
            "items": [{"item_name": item_name, "quantity": 1, "note": "bez cibule"}],
            "order_date": next_weekday.isoformat(),
            "on_behalf_of": other_user.username,
        },
    )
    assert response.status_code == 200, response.text

    target_week = logged_in_client.get(f"/orders/week/{other_user.username}")
    assert target_week.status_code == 200, target_week.text
    target_items = [line["item_name"] for line in target_week.json()]
    assert item_name in target_items

    # ...and the submitting user's own week is untouched.
    own_week = logged_in_client.get(f"/orders/week/{user.username}")
    assert own_week.status_code == 200, own_week.text
    assert item_name not in [line["item_name"] for line in own_week.json()]


def test_self_ordering_unchanged_by_the_new_button(
    logged_in_client, user, menu_week, next_weekday, stub_sheets_sync
):
    """Regression guard: omitting on_behalf_of still orders for yourself."""
    item_name = f"{next_weekday.strftime('%A')} soup"
    response = logged_in_client.post(
        "/orders",
        json={
            "items": [{"item_name": item_name, "quantity": 1, "note": ""}],
            "order_date": next_weekday.isoformat(),
        },
    )
    assert response.status_code == 200, response.text

    own_week = logged_in_client.get(f"/orders/week/{user.username}")
    assert item_name in [line["item_name"] for line in own_week.json()]


def test_admin_send_out_flow_markup_unchanged(admin_client):
    """The admin send-out modal must survive the order-screen edits."""
    html = admin_client.get("/orders").text
    assert 'id="topbar-send-out"' in html
    assert 'id="send-out-modal"' in html
    assert 'id="send-out-confirm"' in html


def test_day_pills_and_summary_total_still_rendered(logged_in_client):
    """Day pills and the summary total are untouched by this task."""
    html = _order_page(logged_in_client)
    assert 'id="day-row"' in html
    assert 'id="summary-total"' in html
    assert 'id="submit-order"' in html
