"""Route-level coverage for the session/redirect chain.

These are the first tests in the suite that exercise real HTTP requests. They
exist to pin the behaviour that the order-modes work is about to change: the
post-login redirect target and the anonymous-visitor gate.

Every assertion here was observed against the running app, not assumed. The
`client` fixture does NOT follow redirects, so a 303 is asserted directly
rather than through the page it lands on.
"""

from app.auth import SESSION_COOKIE_NAME
from tests.conftest import USER_PASSWORD

# The current post-login destination. Task t3 inserts a mode chooser and will
# change this to "/modes" in three coordinated places (pages.py login_page,
# main.py root, login.html's JS). When that lands, this constant is the single
# spot to update -- the surrounding assertions stay valid.
POST_LOGIN_PATH = "/orders"


class TestAnonymousAccess:
    def test_orders_redirects_anonymous_to_login(self, client):
        """An anonymous visitor to the order screen is sent to /login rather
        than getting a bare 401 -- order_page uses get_current_user_optional
        precisely so a browser gets a redirect."""
        response = client.get("/orders")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_login_page_renders_for_anonymous(self, client):
        response = client.get("/login")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_orders_api_rejects_anonymous_with_401(self, client):
        """The JSON API keeps returning 401 (get_current_user), in contrast to
        the HTML page's redirect. Pinned so the two don't get conflated."""
        response = client.get("/orders/my-week")

        assert response.status_code == 401


class TestLoginSetsSession:
    def test_login_sets_session_cookie(self, client, user):
        response = client.post(
            "/login", json={"username": user.username, "password": USER_PASSWORD}
        )

        assert response.status_code == 200
        assert response.json() == {"username": user.username}

        # The cookie is what every subsequent authenticated request depends on.
        assert SESSION_COOKIE_NAME in response.cookies
        assert client.cookies.get(SESSION_COOKIE_NAME)

        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()

    def test_login_persists_a_session_row(self, client, user, db):
        from app.models import UserSession

        client.post("/login", json={"username": user.username, "password": USER_PASSWORD})

        token = client.cookies.get(SESSION_COOKIE_NAME)
        session_row = db.query(UserSession).filter(UserSession.token == token).first()
        assert session_row is not None
        assert session_row.user_id == user.id

    def test_login_with_wrong_password_is_rejected(self, client, user):
        response = client.post(
            "/login", json={"username": user.username, "password": "definitely-wrong"}
        )

        assert response.status_code == 401
        assert SESSION_COOKIE_NAME not in response.cookies

    def test_login_with_unknown_username_is_rejected(self, client):
        response = client.post(
            "/login", json={"username": "nobody", "password": USER_PASSWORD}
        )

        assert response.status_code == 401


class TestLoggedInRedirects:
    def test_login_page_redirects_logged_in_user(self, logged_in_client):
        """A user who already has a session should not be shown the login form
        again. t3 changes the destination to the mode chooser; the redirect
        itself must survive that change."""
        response = logged_in_client.get("/login")

        assert response.status_code == 303
        assert response.headers["location"] == POST_LOGIN_PATH

    def test_orders_renders_for_logged_in_user(self, logged_in_client):
        response = logged_in_client.get("/orders")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_root_redirects_to_post_login_destination(self, client):
        """GET / is a plain RedirectResponse with no explicit status_code, so
        it is 307, not 303. t3 retargets this too."""
        response = client.get("/")

        assert response.status_code == 307
        assert response.headers["location"] == POST_LOGIN_PATH


class TestLogout:
    def test_logout_invalidates_the_session(self, logged_in_client):
        assert logged_in_client.get("/orders").status_code == 200

        response = logged_in_client.post("/logout")
        assert response.status_code == 200

        # Session is gone, so the order page falls back to the anonymous gate.
        assert logged_in_client.get("/orders").status_code == 303


class TestHarnessFixtures:
    """Guards on the harness itself, so later waves inherit fixtures that are
    known-good rather than silently broken."""

    def test_menu_week_is_visible_on_the_order_page(self, logged_in_client, menu_week):
        response = logged_in_client.get("/orders")

        assert response.status_code == 200
        assert "Monday main 1" in response.text

    def test_admin_client_is_admin_and_normal_user_is_not(
        self, client, admin_client, user
    ):
        """/admin/users is require_admin-gated: the admin fixture passes it,
        which is the property t12's review and t13's sweep rely on."""
        assert admin_client.get("/admin/users").status_code == 200

    def test_non_admin_is_forbidden_from_admin_routes(self, logged_in_client):
        response = logged_in_client.get("/admin/users")

        assert response.status_code == 403

    def test_users_endpoint_lists_registered_users(
        self, logged_in_client, other_user
    ):
        """Backs the on-behalf-of selector t8 builds on."""
        response = logged_in_client.get("/users")

        assert response.status_code == 200
        assert other_user.username in response.json()

    def test_sheets_sync_is_stubbed_on_order_submit(
        self, logged_in_client, menu_week, next_weekday, stub_sheets_sync, db
    ):
        """Submitting an order must persist rows and call sync_all -- but the
        stub means nothing reaches Google. t13 asserts the same property."""
        from app.models import Order

        day_name = next_weekday.strftime("%A")
        response = logged_in_client.post(
            "/orders",
            json={
                "order_date": next_weekday.isoformat(),
                "items": [{"item_name": f"{day_name} main 1", "quantity": 1, "note": "bez cibule"}],
            },
        )

        assert response.status_code == 200, response.text
        assert len(stub_sheets_sync) == 1, "sync_all should have been called exactly once"

        stored = db.query(Order).filter(Order.order_date == next_weekday).all()
        assert [(o.item_name, o.note) for o in stored] == [(f"{day_name} main 1", "bez cibule")]
