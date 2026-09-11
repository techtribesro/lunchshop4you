"""Coverage for the mode chooser inserted between login and the order screen.

The chooser is reachable at /modes and is deliberately NOT a one-time gate, so
these tests pin two things together: that the post-login chain now lands on it,
and that the old destination (/orders) is still directly reachable as a deep
link rather than being bounced through the chooser.

The `client` fixture does not follow redirects, so each redirect is asserted on
its own status + Location rather than through the page it lands on. Note the
status codes differ by design: the page routes use an explicit 303, while
main.py's root() is a bare RedirectResponse and therefore 307.
"""


class TestModesPage:
    def test_modes_renders_for_logged_in_user(self, logged_in_client):
        response = logged_in_client.get("/modes")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_modes_offers_both_modes(self, logged_in_client):
        """Both modes must actually be linked, otherwise the chooser renders
        200 while being a dead end."""
        body = logged_in_client.get("/modes").text

        assert 'href="/orders"' in body
        assert 'href="/modes/weekly"' in body

    def test_modes_redirects_anonymous_to_login(self, client):
        response = client.get("/modes")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestModeOnePlaceholder:
    """Mode 1's guided prompt is built in a later task. Until then the card
    must still lead somewhere real -- a 404 would be a broken chooser."""

    def test_weekly_placeholder_renders_for_logged_in_user(self, logged_in_client):
        response = logged_in_client.get("/modes/weekly")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_weekly_placeholder_redirects_anonymous_to_login(self, client):
        response = client.get("/modes/weekly")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestPostLoginChain:
    def test_root_redirects_to_modes(self, client):
        """Bare RedirectResponse -> 307, not 303. The destination changed; the
        status code deliberately did not."""
        response = client.get("/")

        assert response.status_code == 307
        assert response.headers["location"] == "/modes"

    def test_login_page_redirects_logged_in_user_to_modes(self, logged_in_client):
        response = logged_in_client.get("/login")

        assert response.status_code == 303
        assert response.headers["location"] == "/modes"

    def test_login_template_navigates_to_modes(self, client):
        """The submit handler navigates client-side after a fetch POST, so the
        server-side redirects never fire on the real login path. If this string
        regresses to /orders the chooser is silently skipped for every login,
        and no server-side test would catch it."""
        body = client.get("/login").text

        assert 'window.location.href = "/modes"' in body
        assert 'window.location.href = "/orders"' not in body


class TestDeepLinksPreserved:
    def test_orders_still_renders_directly_for_logged_in_user(self, logged_in_client):
        """The chooser sits in front of /orders in the login flow but must not
        gate it: an existing bookmark still renders the grid, not a redirect."""
        response = logged_in_client.get("/orders")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_orders_still_redirects_anonymous_to_login(self, client):
        """The anonymous gate on /orders is unchanged -- it goes to /login, not
        via the chooser."""
        response = client.get("/orders")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_order_page_links_back_to_the_chooser(self, logged_in_client):
        """The chooser is re-reachable, not a one-time gate."""
        assert 'href="/modes"' in logged_in_client.get("/orders").text
