"""Route-level test harness.

The app is server-rendered FastAPI + SQLAlchemy, and every route depends on
`app.db.get_db` plus a `lunch_session` cookie. These fixtures give route tests
a real TestClient against an isolated SQLite database, with the startup side
effects (APScheduler, Google Sheets) neutralised.

Three things have to happen in a specific order, which is why the env var is
set at import time rather than inside a fixture:

1. `app.db` builds `engine`/`SessionLocal` at *module import* from
   `settings.database_path`. By the time any fixture body runs, that engine
   already exists -- so the database path has to be redirected before
   `app.db` is imported for the first time.
2. `app.main`'s lifespan calls `init_db()`, `sync_env_admin()` and
   `start_scheduler()`. The scheduler is patched out in the `app_instance`
   fixture; a real BackgroundScheduler would otherwise outlive the test run.
3. `POST /orders` calls `sheets_sync.sync_all`, which reaches for Google
   credentials. `stub_sheets_sync` replaces it so no test can touch the
   network.
"""

import os
import tempfile
from collections.abc import Iterator
from datetime import date, timedelta

# MUST run before `app.db` is imported -- see module docstring, point 1.
# `Settings` reads DATABASE_PATH from the environment (pydantic-settings maps
# the `database_path` field to that name), so this redirects the engine that
# app.db creates at import time onto a throwaway file.
_TMP_DB_DIR = tempfile.mkdtemp(prefix="lunchshop-tests-")
_TMP_DB_PATH = os.path.join(_TMP_DB_DIR, "test.db")
os.environ["DATABASE_PATH"] = _TMP_DB_PATH
# The env admin is synced on startup from these two settings; blanking them
# makes `sync_env_admin` a documented no-op instead of depending on whatever
# happens to be in a developer's .env.
os.environ["ADMIN_USERNAME"] = ""
os.environ["ADMIN_PASSWORD"] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.db as app_db  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.models import MenuItem, User  # noqa: E402
from app.timezone import today_local, week_start  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _assert_isolated_database() -> None:
    """Guard rail: if the import-time env override ever stops working, the
    suite would silently run against ./data/lunchshop.db (the real dev
    database) and could delete real orders via POST /orders. Fail loudly
    instead."""
    url = str(app_db.engine.url)
    assert _TMP_DB_PATH in url, (
        f"tests are pointed at {url!r}, not an isolated temp database. "
        "Something imported app.db before tests/conftest.py set DATABASE_PATH."
    )


@pytest.fixture
def db_engine():
    """A fresh on-disk SQLite database per test.

    On-disk rather than :memory: because the app code calls `db.commit()` and
    the test body inspects the result through a separate Session; a file-backed
    engine keeps that honest without needing a shared-connection trick.
    """
    fd, path = tempfile.mkstemp(prefix="lunchshop-test-", suffix=".db", dir=_TMP_DB_DIR)
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()
        os.unlink(path)


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(db_session_factory) -> Iterator[Session]:
    """A Session for arrange/assert inside the test body.

    This is deliberately a *different* session from the one the request
    handlers use, so assertions read committed state rather than the test's
    own identity map.
    """
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def stub_sheets_sync(monkeypatch) -> list:
    """Neutralises the Google Sheets mirror and records the calls.

    `sync_all` is invoked by POST /orders and the admin order paths. It builds
    Google API credentials and would attempt network I/O. Returning a call log
    means later tests can assert the sync *was invoked* (the regression sweep
    cares about that) without any call actually leaving the machine.
    """
    calls: list = []

    def _fake_sync_all(session, *args, **kwargs):
        calls.append(session)

    # Patch at every import site: `app.routers.orders` did
    # `from app.services.sheets_sync import sync_all`, so rebinding only the
    # source module would leave the already-bound reference live.
    monkeypatch.setattr("app.services.sheets_sync.sync_all", _fake_sync_all)
    for module in ("app.routers.orders", "app.routers.admin", "app.services.scheduler"):
        try:
            monkeypatch.setattr(f"{module}.sync_all", _fake_sync_all)
        except AttributeError:
            # Module doesn't import sync_all by name; the source patch covers it.
            pass
    return calls


@pytest.fixture
def app_instance(db_session_factory, stub_sheets_sync, monkeypatch):
    """The real FastAPI app, wired to the isolated database.

    The lifespan is left running (so the app is exercised the way production
    starts it) but `start_scheduler` is replaced -- a real BackgroundScheduler
    would spawn a thread that outlives the test and fire IMAP/Groq jobs.
    """
    import app.main as app_main

    class _DummyScheduler:
        def shutdown(self, wait: bool = False) -> None:  # matches lifespan's call
            pass

    monkeypatch.setattr(app_main, "start_scheduler", lambda: _DummyScheduler())
    # The lifespan also opens `SessionLocal()` for sync_env_admin; point that at
    # the per-test database so startup can't touch the developer's real file.
    monkeypatch.setattr(app_main, "SessionLocal", db_session_factory)

    fastapi_app = app_main.app

    def _override_get_db() -> Iterator[Session]:
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client(app_instance) -> Iterator[TestClient]:
    """Anonymous client. Redirects are NOT followed, so tests can assert on the
    redirect itself (status + Location) rather than the destination page."""
    with TestClient(app_instance, follow_redirects=False) as test_client:
        yield test_client


USER_PASSWORD = "user-pw"
ADMIN_PASSWORD = "admin-pw"


def _make_user(session: Session, username: str, password: str, is_admin: bool) -> User:
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def user(db) -> User:
    """A normal, non-admin user. Password is `USER_PASSWORD`."""
    return _make_user(db, "tester", USER_PASSWORD, is_admin=False)


@pytest.fixture
def admin_user(db) -> User:
    """An admin user. Password is `ADMIN_PASSWORD`."""
    return _make_user(db, "admin", ADMIN_PASSWORD, is_admin=True)


@pytest.fixture
def other_user(db) -> User:
    """A second normal user -- the on-behalf-of target for later tests."""
    return _make_user(db, "colleague", USER_PASSWORD, is_admin=False)


def login_as(test_client: TestClient, username: str, password: str = USER_PASSWORD) -> TestClient:
    """Log a client in through the real POST /login, so the session cookie is
    created by the application's own code path rather than a hand-rolled row."""
    response = test_client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200, f"login failed: {response.status_code} {response.text}"
    return test_client


@pytest.fixture
def logged_in_client(client, user) -> TestClient:
    """Client carrying a valid session cookie for the non-admin `user`."""
    return login_as(client, user.username, USER_PASSWORD)


@pytest.fixture
def admin_client(client, admin_user) -> TestClient:
    """Client carrying a valid session cookie for `admin_user`."""
    return login_as(client, admin_user.username, ADMIN_PASSWORD)


@pytest.fixture
def menu_week(db) -> list[MenuItem]:
    """Seeds a menu for the current week, matching the shape order_page queries
    (week_start == week_start(today_local()), English day names as stored by the
    parser). Monday..Friday with a soup and two mains, so a lettered a/b/c
    prompt has something to iterate over.
    """
    ws = week_start(today_local())
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    items: list[MenuItem] = []
    for offset, day in enumerate(day_names):
        items.append(
            MenuItem(
                week_start=ws,
                day=day,
                category="Polevka",
                item_name=f"{day} soup",
                description="test soup",
                price_czk=35,
                calories_kcal=120,
            )
        )
        for index in (1, 2):
            items.append(
                MenuItem(
                    week_start=ws,
                    day=day,
                    category=f"Hlavni jidlo {index}",
                    item_name=f"{day} main {index}",
                    description="test main",
                    price_czk=135 + index,
                    calories_kcal=600 + index,
                )
            )
        del offset
    db.add_all(items)
    db.commit()
    return items


@pytest.fixture
def next_weekday() -> date:
    """An orderable date: today if today is a weekday, otherwise the coming
    Monday. `_check_ordering_allowed` rejects past dates and weekends, so tests
    that submit an order need a date that satisfies both rules whenever the
    suite happens to run."""
    today = today_local()
    if today.weekday() < 5:
        return today
    return today + timedelta(days=7 - today.weekday())
