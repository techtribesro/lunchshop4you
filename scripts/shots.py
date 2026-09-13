#!/usr/bin/env python
"""Screenshot + accessibility evidence harness for lunchshop4you.

DEV TOOLING ONLY. Nothing under app/ imports this module, and playwright is
a dev-extra dependency (see pyproject.toml [project.optional-dependencies]
.dev) so it never ships to Fly.io.

WHAT IT DOES
------------
For each requested route, captures:
  * a WIDE  screenshot at 1440x900   -> <out>/<name>__<theme>__wide_1440x900.png
  * a NARROW screenshot at 390x844   -> <out>/<name>__<theme>__narrow_390x844.png
  * an ARIA/accessibility snapshot   -> <out>/<name>__<theme>__a11y.yaml
(optionally in both light and dark theme, see --themes)

It starts its own uvicorn server against a THROWAWAY SQLite database, seeds
a normal user + an admin user + a full menu week, logs in through the real
login form, and shuts the server down afterwards. It never touches your dev
database and never talks to production.

QUICK START
-----------
One-time setup (per checkout):

    .venv/bin/python -m pip install -e '.[dev]'
    .venv/bin/python -m playwright install chromium

Capture the default set of routes:

    .venv/bin/python scripts/shots.py

Capture specific routes into a named evidence folder:

    .venv/bin/python scripts/shots.py \
        --route /login --route /orders --route /modes \
        --out ../evidence/t9 --themes light,dark

COMMON OPTIONS
--------------
  --route PATH        Repeatable. Defaults to /login and /orders.
                      Use `--route /modes:chooser` to force the output
                      filename stem to "chooser" instead of deriving it.
  --out DIR           Where PNGs/JSON land. Default: the run's evidence dir
                      (see --out resolution below).
  --themes LIST       Comma-separated subset of {light,dark}. Default: light.
  --no-login          Capture anonymous (logged-out) pages instead.
  --admin             Log in as the seeded ADMIN instead of the normal user.
                      Required for admin-only chrome: the "Odeslat
                      objednavku" CTA is gated on {% if user.is_admin %}
                      (app/templates/app.html) and is absent from the DOM
                      entirely for a normal user. Default: normal user.
  --port N            Bind port. Default: an auto-picked free port.
  --base-url URL      Attach to an ALREADY-RUNNING server instead of
                      starting one (no seeding, no teardown).
  --full-page         Capture the whole scrollable page, not just viewport.
  --keep-db           Don't delete the throwaway SQLite file (debugging).

OUT DIR RESOLUTION
------------------
--out wins. Otherwise $LIBERTA_EVIDENCE_DIR. Otherwise, if this checkout is
a Liberta worktree (…/waves/<n>/worktrees/<task>), the run's
`evidence/<task>/` directory. Otherwise ./evidence.

EXIT CODE
---------
0 only if every requested capture produced a PNG at each viewport. Any
failure (server never came up, login failed, route errored) exits non-zero
with the reason on stderr -- so CI / a style task can trust the exit code.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WIDE = {"width": 1440, "height": 900}
NARROW = {"width": 390, "height": 844}

# Seeded credentials -- throwaway DB only, never real secrets. These are
# test fixtures, not production values.
SEED_USER = "stylist"
SEED_PASSWORD = "stylist-pw"
SEED_ADMIN = "styleadmin"
SEED_ADMIN_PASSWORD = "styleadmin-pw"

DEFAULT_ROUTES = ["/login", "/orders"]


# --------------------------------------------------------------------------
# output location
# --------------------------------------------------------------------------
def resolve_out_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("LIBERTA_EVIDENCE_DIR")
    if env:
        return Path(env).expanduser().resolve()

    # …/<run>/waves/<n>/worktrees/<task>  ->  …/<run>/evidence/<task>
    parts = REPO_ROOT.parts
    if "worktrees" in parts and "waves" in parts:
        idx = parts.index("waves")
        run_dir = Path(*parts[:idx])
        task = REPO_ROOT.name
        return run_dir / "evidence" / task

    return REPO_ROOT / "evidence"


# --------------------------------------------------------------------------
# throwaway database seeding
# --------------------------------------------------------------------------
def seed_database(db_path: Path) -> None:
    """Create the schema and populate a user, an admin and a full menu week.

    Imported lazily and with DATABASE_PATH already pointed at the throwaway
    file, so app.db binds its engine to that file and never to the real one.
    """
    from app.auth import hash_password
    from app.db import SessionLocal, init_db
    from app.models import MenuItem, User
    from app.timezone import menu_target_week_start

    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == SEED_USER).first() is None:
            db.add(User(username=SEED_USER, password_hash=hash_password(SEED_PASSWORD), is_admin=False))
        if db.query(User).filter(User.username == SEED_ADMIN).first() is None:
            db.add(
                User(
                    username=SEED_ADMIN,
                    password_hash=hash_password(SEED_ADMIN_PASSWORD),
                    is_admin=True,
                )
            )
        db.commit()

        # Seed the week the PAGES ACTUALLY RENDER. app/routers/pages.py and
        # orders.py both target menu_target_week_start(); on a weekend that is
        # the UPCOMING week, while week_start() is the outgoing one. Seeding
        # week_start() here would populate a week no page displays, and the
        # screenshots would show an empty menu that looks like a product bug.
        ws = menu_target_week_start()
        if db.query(MenuItem).filter(MenuItem.week_start == ws).first() is None:
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
            for offset, day in enumerate(days):
                db.add(
                    MenuItem(
                        week_start=ws,
                        day=day,
                        category="Polevka",
                        item_name=f"Polévka {offset + 1}",
                        description="Vývar se zeleninou",
                        price_czk=45,
                        calories_kcal=120,
                    )
                )
                for variant in (1, 2, 3):
                    db.add(
                        MenuItem(
                            week_start=ws,
                            day=day,
                            category=f"Hlavni jidlo {variant}",
                            item_name=f"{day[:2]} Hlavní jídlo {variant}",
                            description="Příloha dle denní nabídky",
                            price_czk=135 + variant * 5,
                            calories_kcal=600 + variant * 40,
                        )
                    )
            db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# server lifecycle
# --------------------------------------------------------------------------
def pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(base_url: str, proc: subprocess.Popen | None, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"uvicorn exited early with code {proc.returncode}. "
                f"Check the server log above for the traceback."
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:  # not up yet
            last_err = exc
        time.sleep(0.3)
    raise RuntimeError(f"Server did not become healthy at {base_url} within {timeout}s: {last_err}")


def start_server(port: int, db_path: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATABASE_PATH"] = str(db_path)
    # Keep the throwaway instance inert: no admin auto-provisioning from a
    # developer's real .env, no outbound integrations.
    env["ADMIN_USERNAME"] = ""
    env["ADMIN_PASSWORD"] = ""
    env["GMAIL_IMAP_USER"] = ""
    env["GMAIL_IMAP_PASSWORD"] = ""
    env["GOOGLE_SHEETS_SPREADSHEET_ID"] = ""
    env["TELEGRAM_BOT_TOKEN"] = ""
    env["GROQ_API_KEY"] = ""

    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),  # app mounts app/static and app/templates relatively
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
def parse_route(spec: str) -> tuple[str, str]:
    """'/orders' -> ('/orders','orders'); '/modes:chooser' -> ('/modes','chooser')."""
    if ":" in spec:
        path, _, name = spec.partition(":")
    else:
        path, name = spec, ""
    if not path.startswith("/"):
        path = "/" + path
    if not name:
        name = path.strip("/").replace("/", "_") or "root"
    return path, name


def login(context, base_url: str, username: str = SEED_USER,
          password: str = SEED_PASSWORD) -> None:
    """Log in via the real form so the session cookie is set exactly as a
    user's would be.

    Defaults to the non-admin SEED_USER so existing evidence stays
    reproducible; pass the admin credentials (see --admin) to capture
    admin-only chrome such as the .send-out-btn CTA, which app.html gates
    behind `{% if user.is_admin %}` and which simply does not exist in the
    DOM for a normal user.
    """
    page = context.new_page()
    try:
        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        page.fill("#u", username)
        page.fill("#p", password)
        # login.html posts /login via fetch() and THEN assigns
        # window.location.href = "/modes". There is no form navigation, so
        # expect_navigation() has no event to catch and times out.
        page.click("button[type=submit]")
        page.wait_for_url("**/modes", timeout=15000)
        if "/login" in page.url:
            error = page.text_content("#login-error") or ""
            raise RuntimeError(
                f"Login as {username!r} failed, still on /login. "
                f"Page error: {error.strip()!r}"
            )
    finally:
        page.close()


def capture_route(context, base_url: str, path: str, name: str, theme: str,
                  out_dir: Path, full_page: bool) -> list[Path]:
    written: list[Path] = []
    for label, viewport in (("wide_1440x900", WIDE), ("narrow_390x844", NARROW)):
        page = context.new_page()
        try:
            page.set_viewport_size(viewport)
            if theme == "dark":
                page.emulate_media(color_scheme="dark")
                page.add_init_script(
                    "document.documentElement.setAttribute('data-theme','dark')"
                )
            elif theme == "light":
                page.emulate_media(color_scheme="light")

            resp = page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=30000)
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"{path} returned HTTP {resp.status}")

            # Guard against silently screenshotting the wrong page. This app
            # redirects by auth state in both directions (/login -> /orders
            # when logged in; /orders -> /login when anonymous), so without
            # this check a "login" PNG can actually be the order grid.
            landed = page.evaluate("location.pathname")
            if landed.rstrip("/") != path.rstrip("/"):
                raise RuntimeError(
                    f"{path} redirected to {landed}. Evidence would be mislabelled. "
                    f"Hint: use --no-login for pages only reachable while logged out "
                    f"(e.g. /login), and log in for pages that require a session."
                )
            if theme == "dark":
                page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
            page.wait_for_timeout(350)  # let fonts/late JS settle

            shot = out_dir / f"{name}__{theme}__{label}.png"
            page.screenshot(path=str(shot), full_page=full_page)
            written.append(shot)

            if label.startswith("wide"):
                # NOTE: page.accessibility.snapshot() was REMOVED in
                # playwright >= 1.55 (long deprecated). The supported
                # equivalent is aria_snapshot(), which yields the ARIA tree
                # as YAML -- roles plus accessible names, which is exactly
                # what "is this a named, focusable control?" needs.
                snapshot = page.locator("body").aria_snapshot()
                a11y = out_dir / f"{name}__{theme}__a11y.yaml"
                a11y.write_text(snapshot, encoding="utf-8")
                written.append(a11y)
        finally:
            page.close()
    return written


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture wide/narrow screenshots + a11y snapshots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See the module docstring for full usage examples.",
    )
    parser.add_argument("--route", action="append", default=None,
                        help="Route to capture, repeatable. '/path' or '/path:name'.")
    parser.add_argument("--out", default=None, help="Output directory.")
    parser.add_argument("--themes", default="light", help="Comma list: light,dark")
    parser.add_argument("--no-login", action="store_true", help="Capture logged-out.")
    parser.add_argument("--admin", action="store_true",
                        help="Log in as the seeded ADMIN user instead of the normal "
                             "user, so admin-only chrome (the 'Odeslat objednavku' "
                             "CTA) renders. Default: the normal user.")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--base-url", default=None,
                        help="Attach to a running server instead of starting one.")
    parser.add_argument("--full-page", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. Run:\n"
              "  .venv/bin/python -m pip install -e '.[dev]'\n"
              "  .venv/bin/python -m playwright install chromium", file=sys.stderr)
        return 2

    routes = [parse_route(r) for r in (args.route or DEFAULT_ROUTES)]
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    for t in themes:
        if t not in ("light", "dark"):
            print(f"Unknown theme {t!r}; use light and/or dark.", file=sys.stderr)
            return 2

    out_dir = resolve_out_dir(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    proc: subprocess.Popen | None = None
    tmp_dir: Path | None = None
    written: list[Path] = []

    try:
        if args.base_url:
            base_url = args.base_url.rstrip("/")
            print(f"[shots] attaching to existing server at {base_url}")
            wait_for_health(base_url, None)
        else:
            tmp_dir = Path(tempfile.mkdtemp(prefix="shots-"))
            db_path = tmp_dir / "shots.db"
            log_path = tmp_dir / "uvicorn.log"

            # Seed BEFORE the server starts so the app boots against a
            # populated DB (and so seeding errors surface immediately).
            os.environ["DATABASE_PATH"] = str(db_path)
            sys.path.insert(0, str(REPO_ROOT))
            seed_database(db_path)

            port = args.port or pick_free_port()
            base_url = f"http://127.0.0.1:{port}"
            print(f"[shots] starting uvicorn on {base_url} (db={db_path})")
            proc = start_server(port, db_path, log_path)
            try:
                wait_for_health(base_url, proc)
            except Exception:
                if log_path.exists():
                    sys.stderr.write(log_path.read_text(errors="replace")[-4000:])
                raise
            print("[shots] server healthy")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context()
                if not args.no_login:
                    username, password = (
                        (SEED_ADMIN, SEED_ADMIN_PASSWORD) if args.admin
                        else (SEED_USER, SEED_PASSWORD)
                    )
                    login(context, base_url, username, password)
                    print(f"[shots] logged in as {username}")

                for path, name in routes:
                    for theme in themes:
                        print(f"[shots] capturing {path} ({theme})")
                        written += capture_route(
                            context, base_url, path, name, theme, out_dir, args.full_page
                        )
            finally:
                browser.close()

    except Exception as exc:
        print(f"[shots] FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            stop_server(proc)
        if tmp_dir is not None and not args.keep_db:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    pngs = [p for p in written if p.suffix == ".png"]
    expected = len(routes) * len(themes) * 2
    print(f"\n[shots] wrote {len(pngs)} PNG(s) to {out_dir}")
    for path in written:
        print(f"  {path}")
    if len(pngs) != expected:
        print(f"[shots] FAILED: expected {expected} PNGs, got {len(pngs)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
