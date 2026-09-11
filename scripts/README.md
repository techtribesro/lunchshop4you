# scripts/ — developer tooling

Not part of the deployed application. Nothing under `app/` imports anything
here, and these scripts are never installed into the Fly.io image.

## shots.py — screenshot & accessibility evidence harness

Produces the visual evidence that `style` tasks are required to attach: a
**wide (1440x900)** and a **narrow (390x844)** PNG per route, plus an
accessibility snapshot.

### One-time setup

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
```

`playwright` is a **dev extra only** (`[project.optional-dependencies].dev`
in `pyproject.toml`). It must never be moved into the runtime
`dependencies` list — this app runs on Fly.io and must not grow a browser
dependency in production.

### Usage

```bash
# default routes (/login, /orders), light theme
.venv/bin/python scripts/shots.py

# what a style task typically wants
.venv/bin/python scripts/shots.py \
    --route /login --route /modes --route /orders \
    --themes light,dark \
    --out ../../../evidence/t9
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--route PATH` | Repeatable. `--route /modes:chooser` forces the filename stem. |
| `--themes light,dark` | Which theme states to capture. Default `light`. |
| `--out DIR` | Output directory (see resolution order below). |
| `--no-login` | Capture the logged-out view instead. |
| `--full-page` | Whole scrollable page rather than just the viewport. |
| `--base-url URL` | Attach to an already-running server; skips seeding/teardown. |
| `--port N` | Fixed port instead of an auto-picked free one. |
| `--keep-db` | Keep the throwaway SQLite file for debugging. |

### What it does for you

The script is self-contained — a style producer should not need to start a
server by hand:

1. Creates a **throwaway SQLite database** in a temp dir (`DATABASE_PATH` is
   overridden, so your real `data/lunchshop.db` is never touched).
2. Seeds a normal user (`stylist`), an admin (`styleadmin`), and a full
   Mon–Fri menu for the current week, so `/orders` renders with real content
   instead of an empty state.
3. Starts its own `uvicorn` on a free port with integrations blanked out (no
   Gmail, Sheets, Telegram or Groq calls), waits for `/health`, and **shuts
   it down again** when finished.
4. Logs in through the real login form, so the session cookie is exactly
   what a user would get.
5. Captures both viewports per route, writes an ARIA snapshot, and exits
   non-zero if any capture failed.

> **Accessibility API note.** `page.accessibility.snapshot()` was *removed*
> in Playwright ≥ 1.55 (it had long been deprecated), so the harness uses
> `aria_snapshot()` instead. It emits the ARIA tree as YAML — roles plus
> accessible names, e.g. `- button "Objednat"` / `- textbox "Poznámka"` —
> which is what the wave-4 gates actually assert on ("the lettered options
> are real focusable controls with accessible names").

### Output

```
<out>/<name>__<theme>__wide_1440x900.png
<out>/<name>__<theme>__narrow_390x844.png
<out>/<name>__<theme>__a11y.yaml
```

`--out` resolution order: `--out` → `$LIBERTA_EVIDENCE_DIR` → the run's
`evidence/<task>/` directory when run from a Liberta worktree → `./evidence`.

### Exit code

`0` only when every requested route produced a PNG at **both** viewports.
Any failure (server never came up, login rejected, route returned 4xx/5xx)
exits non-zero with the reason on stderr, so it is safe to gate a task on.
