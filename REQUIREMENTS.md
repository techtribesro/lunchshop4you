# Lunch Ordering Web App – Requirements

**Last updated:** August 12, 2026
**Timezone:** CET/CEST (Prague)
**Users:** 6–7, one office
**Stack:** Python 3.11 / FastAPI, server-rendered Jinja2 templates + vanilla JS, SQLite, Fly.io

This document describes the app as it actually runs in production. It supersedes the
original spec (kept in git history) — the two biggest deviations are noted inline where
they matter: **SQLite instead of Google Sheets as the datastore**, and **session cookies
instead of JWT**.

---

## 1. Menu Ingestion

- A `BackgroundScheduler` (APScheduler) job polls Gmail via IMAP every **Sunday at 18:00
  Prague time**, scanning the most recent messages in the inbox (optionally filtered by
  `MENU_EMAIL_SENDER`) for the weekly menu.
- If the email has a **PDF attachment**, the whole PDF is sent to **Gemini**
  (`gemini-flash-latest`, raw REST call, no SDK) with a structured JSON response schema.
  Gemini returns, per item: day, category, name, description, price (CZK), and an
  **estimated calorie count** — extraction and calorie estimation happen in the **same
  API call**, not two, to stay inside Gemini's free-tier quota (see §6).
- If there's no PDF (rare, plain-text fallback), the body is parsed with a regex-based
  parser (`app/services/menu_parser.py`) that splits on the Czech day headers (PONDĚLÍ,
  ÚTERÝ, STŘEDA, ČTVRTEK, PÁTEK) and category labels, then calorie counts for that path
  are estimated with a **second, separate** Gemini call (`calorie_estimator.py`) since
  there's no PDF call to piggyback on.
- Categories: Polévka (soup), Hlavní jídlo 1–3 (mains), Vege. jídlo (vegetarian). Not
  every day has every category.
- **Standing discount:** the office has a negotiated 50 CZK discount off every dish
  except soup. Applied automatically to `price_czk` at parse time
  (`email_poller.NON_SOUP_DISCOUNT_CZK`), for both the PDF and text-fallback paths, so
  it's in effect on every future parse without manual intervention.
- Each new parse **replaces the current week's menu** (delete + bulk insert), keyed by
  `week_start`.
- An admin can also trigger a re-parse on demand (`POST /admin/parse-menu`), or wipe the
  current week's menu without re-fetching (`POST /admin/clear-menu`) to clear a bad parse
  before retrying.

## 2. Ordering

- Login required (username + password).
- The order page shows all five weekdays as day pills; the current day is marked, and an
  admin-opened "early" day (see below) is marked separately. Each day's menu shows every
  category, with price and estimated kcal per item.
- Users pick quantities per item and may attach a **free-text note per item** (e.g. "bez
  cibule"), up to 255 characters.
- Submitting **replaces** that user's order for that date in full (delete-then-insert) —
  latest submission wins, no edit history.
- **Cutoff:** 11:00 CET/CEST, configurable via `ORDER_CUTOFF_TIME`. The UI shows a
  live "open / closing soon (≤15 min) / closed" pill. The server independently enforces
  the same cutoff — the client-side state is UI-only.
- **Early ordering:** an admin can open ordering for the *next business day* ahead of its
  own cutoff (`POST /admin/early-ordering/open`), e.g. the evening before. This is scoped
  to the case where that next business day falls in the already-loaded week — if it would
  cross into a week whose menu hasn't been parsed yet, opening is rejected. Each user's
  cart is tracked per-date client-side so today's and an early-opened day's selections
  don't collide.
- No weekend ordering.

## 3. Dashboard

- Visible to all logged-in users, recomputed on every request from SQLite (no caching).
- Per user and aggregate: daily / weekly / monthly spend (CZK) **and** estimated calorie
  totals (kcal), the latter computed by joining orders back to the menu item they were
  priced against.
- Aggregation logic lives in `app/services/dashboard.py` and is shared between the
  dashboard endpoint and the Google Sheets sync (§5), so both stay consistent.

## 4. Authentication

- Username + password login, session identified by a signed cookie backed by a
  `sessions` table in SQLite (not stateless JWT — see §7 for why).
- Idle timeout: `SESSION_IDLE_TIMEOUT_HOURS` (default 24h).
- **Admin account** is provisioned automatically on every app boot from
  `ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars (upserted, password re-hashed each boot) —
  no manual bootstrap step needed on a fresh deploy.
- Additional users and admin-toggling are managed from the in-app admin panel
  (`/admin/users`), not a CLI. `app/cli.py` (`create-user`, `reset-password`,
  `make-admin`) still exists as a fallback ops tool.
- An admin cannot revoke their own admin flag via the toggle endpoint (guards against
  locking everyone out).

## 5. Admin Panel

Gated by `is_admin` (`require_admin` dependency, 403 otherwise). Available actions:

- Force a menu re-parse, or clear the current week's menu.
- Manually trigger the daily order-summary email.
- View / open / close the early-ordering window for the next business day.
- List users, create a user (with optional admin flag), reset a password, toggle a
  user's admin status.
- **Assign a meal to a user directly**: pick a user and a weekday of the current week,
  set item quantities the same way the order screen does, and save — this replaces that
  user's order for that date (same delete-then-insert semantics as self-service
  ordering). Unlike normal ordering, this **bypasses the cutoff and early-ordering
  rules entirely** — it exists specifically to fill in an order for someone who forgot,
  is out of office, etc., not to route around the schedule for yourself.

## 6. Calorie Estimation

- Estimated kcal per dish is shown on menu item cards, in the order summary panel (a
  running total for the current cart, including early-ordering carts), and in
  weekly/monthly dashboard totals.
- Sourced from Gemini as part of the PDF-extraction call described in §1 — **not** a
  separate call — because Gemini's free tier caps at **20 requests/day per project per
  model**, shared across menu parsing and (rarely) the text-fallback calorie call. A
  second daily consumer (e.g. Gemini-generated order-summary copy) was deliberately
  removed for the same reason — see §8.
- Values are best-effort LLM estimates, not authoritative nutrition data.
- **Manual backfill:** `POST /admin/menu/calories` lets an admin set `calories_kcal` for
  specific (day, item_name) rows in the current week directly, bypassing Gemini
  entirely. This exists for cases like a menu parsed before the kcal feature existed (so
  every row is `null`), or when quota is tight and rough hand-entered estimates are good
  enough for the day — no Gemini call spent, and the next scheduled/triggered re-parse
  overwrites these with real Gemini estimates anyway.
- **Manual price backfill:** `POST /admin/menu/prices` is the equivalent for
  `price_czk` — sets absolute prices for specific (day, item_name) rows. Deliberately
  absolute rather than relative/incremental: an earlier relative "subtract 50" version
  compounded every time it was retried after an apparent failure and zeroed out a
  week's prices in production (see §9) — an idempotent "set to X" endpoint can't repeat
  that failure mode.

## 7. Authentication Architecture Note

The original spec called for stateless JWT cookies. This was changed to a signed cookie
+ server-side `sessions` table because idle-timeout expiry (log the user out after N
hours of *inactivity*, not N hours after login) isn't something a stateless JWT can do
cleanly without extra bookkeeping that ends up being a session table anyway.

---

## Non-Functional Requirements

- **Users:** 6–7 concurrent.
- **Hosting:** Fly.io, region `fra`, shared-cpu-1x, **512 MB RAM** (see §9 — bumped back
  up after a second OOM incident; 256 MB is not currently reliable).
- **Timezone:** Europe/Prague for all scheduling and cutoff logic.
- **Uptime:** best-effort; single always-on machine, no HA.

---

## Data Storage

### SQLite — operational datastore (source of truth)

A single SQLite file on a persistent Fly volume (`lunchshop_data`, mounted at `/data`,
`DATABASE_PATH=/data/lunchshop.db`). Tables (see `app/models.py`):

| Table | Purpose |
|---|---|
| `users` | username, password hash, `is_admin`, created_at |
| `sessions` | session token → user, created_at, last_seen_at (idle-timeout tracking) |
| `menu` | week_start, day, category, item_name, description, price_czk, `calories_kcal`, parsed_at — unique on (week_start, day, category, item_name) |
| `orders` | user_id, order_date, week_start, item_name, quantity, unit_price_czk, `note`, submitted_at — unique on (user_id, order_date, item_name) |
| `early_ordering_windows` | order_date (unique), opened_by, opened_at |

Schema changes are applied additively at startup: `init_db()` runs
`Base.metadata.create_all()` and then a small routine that diffs each model's columns
against the live table and issues `ALTER TABLE ... ADD COLUMN` for anything missing —
there's no Alembic; this only ever adds nullable/defaulted columns, never drops or
alters existing ones.

### Google Sheets — reporting mirror (not the datastore)

`app/services/sheets_sync.py` pushes one-way snapshots of `menu`, `orders`, and a
computed `dashboard` tab to a Google Sheet after every menu parse, using the shared
`compute_dashboard()` logic from §3. Each sync clears and rewrites all three tabs — no
stale rows, no incremental diffing. The app **never reads back** from Sheets; it exists
purely so the data is human-browsable/auditable outside the app.

- Auth: a Google Cloud service account
  (`lunchshop4you@gen-lang-client-0404501894.iam.gserviceaccount.com`), granted Editor
  access to the target spreadsheet.
- Credentials are supplied either as a local JSON key file path
  (`GOOGLE_SERVICE_ACCOUNT_JSON`, for local dev) or as inline JSON in the same env var
  (for Fly.io, which only supports env-var secrets, not file mounts).
- If `GOOGLE_SHEETS_SPREADSHEET_ID` or credentials aren't configured, sync is skipped
  with a warning log — the app functions fully without it.

---

## Email

### Inbound: weekly menu (Gmail IMAP)

- `GMAIL_IMAP_HOST` / `GMAIL_IMAP_USER` / `GMAIL_IMAP_PASSWORD` (Gmail app password),
  optionally scoped to `MENU_EMAIL_SENDER`.
- Scans the most recent messages in the inbox (newest first) for a PDF attachment or
  plain-text body; see §1 for parsing.
- RFC2047-encoded attachment filenames are decoded before the `.pdf` extension check
  (vendor emails send encoded-word filenames).

### Outbound: daily order summary (Gmail SMTP)

- Sent Mon–Fri shortly after cutoff, `ORDER_SUMMARY_SEND_TIME` (default 11:05, five
  minutes after the 11:00 cutoff to let last-second edits settle).
- Recipient: `ORDER_SUMMARY_RECIPIENT_EMAIL` / `ORDER_SUMMARY_RECIPIENT_NAME` (the
  restaurant contact, "Honza"). Sent from the app's single Gmail account
  (`GMAIL_IMAP_USER`, also used for inbound menu polling), with the display name set by
  `ORDER_SUMMARY_SENDER_NAME` (default "golfshop4you") — used both in the `From` header,
  the subject line (`golfshop4you – Objednávka obědů – <date>`), and the closing
  "Děkujeme," sign-off.
- Body is a **single** HTML table, one row per dish, columns: Jídlo (dish), Počet
  (total quantity), Kdo (each buyer with their quantity, e.g. "Yakob ×2, Toan ×1"),
  Poznámky (per-item notes, e.g. allergies/exclusions — rendered in a **red-bordered
  box** so they visually stand out to the restaurant). An earlier version sent two
  tables (a per-person list plus a separate per-dish summary); this was collapsed to one
  table per explicit request — the restaurant only needs to know what to prepare, how
  many, for whom, and what to watch out for, in one place. Sent via STARTTLS on port
  587 — no `.xlsx` attachment, no spreadsheet dependency.
- **The email copy (greeting/intro/thanks) is a static, hardcoded template**
  (`app/services/order_summary.py::EMAIL_COPY`), not Gemini-generated. This was a
  deliberate call: this email fires automatically every weekday, and spending a Gemini
  call on it every single day doesn't fit the 20-req/day free-tier budget alongside menu
  parsing. Not grammatically perfect Czech vocative for an arbitrary name, but the
  recipient name is fixed per deployment, so it only has to read right once.
- Also triggerable on demand from the admin panel.

### Additional: Telegram broadcast

- Fires immediately after the email succeeds, as an **additional** channel — not a
  replacement. If the restaurant email doesn't land for some reason, anyone subscribed
  can copy-paste the Telegram message and forward it to Honza manually.
- Bot: `@lunchshop4you_bot`. Anyone who messages it is auto-subscribed via a webhook
  (`POST /telegram/webhook`, validated against `TELEGRAM_WEBHOOK_SECRET`) — no manual
  chat-ID lookup needed; the bot replies with a confirmation and adds them to the
  `telegram_subscribers` table. The daily broadcast goes to everyone in that table,
  grouped by person (name, then their item/qty lines) rather than by dish — read better
  on a phone than the per-dish table used in the email.
- Admin panel has a "Telegram odběratelé" card to view/remove subscribers.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` are Fly secrets, not plain
  `fly.toml` env vars (they're credentials — same treatment as
  `GOOGLE_SERVICE_ACCOUNT_JSON`).
- Failures here are logged and swallowed, never allowed to affect the email path or the
  caller's response — see `_sync_all_best_effort`-style pattern used elsewhere in
  `admin.py` for the same reasoning.

---

## API/Endpoints

### Pages (server-rendered)
- `GET /login` — login form, redirects to `/orders` if already authenticated
- `GET /orders` — main app page (menu, cart, dashboard, admin panel if `is_admin`)

### Auth
- `POST /login` — username + password → session cookie
- `POST /logout` — destroy session

### Menu
- `GET /menu/today` — current day's menu
- `GET /menu/week` — full week menu

### Orders
- `POST /orders` — submit/replace an order for a given date (today, or an
  early-opened date)
- `GET /orders/my-week` — caller's current-week orders

### Dashboard
- `GET /dashboard` — all users' daily/weekly/monthly spend + kcal totals

### Admin (all require `is_admin`)
- `POST /admin/parse-menu` — force a menu re-parse
- `POST /admin/clear-menu` — wipe current week's menu without re-fetching
- `POST /admin/send-order-summary` — send today's summary email on demand
- `GET /admin/early-ordering` — status for the next business day
- `POST /admin/early-ordering/open` / `close`
- `GET /admin/users` — list users
- `POST /admin/users` — create a user
- `POST /admin/users/{username}/reset-password`
- `POST /admin/users/{username}/toggle-admin`
- `POST /admin/menu/calories` — manually set kcal for specific current-week menu items
  (see §6)
- `POST /admin/menu/prices` — manually set absolute prices for specific current-week
  menu items (see §6)
- `GET /admin/orders` — look up a specific user's order for a given date (`username`,
  `order_date` query params)
- `POST /admin/orders` — set (replace) a user's order for a given date on their behalf,
  bypassing cutoff (see §5)
- `DELETE /admin/orders/by-date` — wipe every user's order for a given date (`order_date`
  query param) — for clearing out test/junk data
- `GET /admin/telegram-subscribers` — list everyone subscribed to the Telegram broadcast
- `DELETE /admin/telegram-subscribers/{chat_id}` — remove a subscriber

### Telegram
- `POST /telegram/webhook` — Telegram calls this on every message to the bot; not
  admin-gated (it's called by Telegram, not a logged-in user), validated instead via the
  `X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`

### Ops
- `GET /health`

---

## Constraints & Decisions

- **11:00 CET/CEST hard cutoff**, enforced server-side; UI cutoff pill is advisory only.
- **No payment processing** — prices are for tracking/billing the office, not charged
  in-app.
- **Plain-text/PDF email parsing**, no assumption of consistent HTML structure from the
  vendor.
- **Single timezone** (Europe/Prague) for all users and scheduling.
- **SQLite as source of truth, Sheets as a mirror** — see §5 above; this is the biggest
  deviation from the original spec, which called for Sheets as the primary datastore.
  SQLite was chosen because 6–7 users generate negligible write volume, and a real
  relational store makes the cutoff/early-ordering/kcal-aggregation logic far simpler
  than driving it off the Sheets API.
- **Delete-and-reinsert semantics**: both menu refresh and order submission
  replace-in-full rather than diff/patch — simpler, and correct at this scale.

---

## Infrastructure

### Hosting: Fly.io
- App `lunchshop4you`, region `fra`, `shared-cpu-1x`, **512 MB RAM**.
- Persistent volume `lunchshop_data` mounted at `/data` for the SQLite file.
- `min_machines_running = 1`, `auto_stop_machines = false` — always-on, no cold starts.
- Secrets requiring inline JSON (`GOOGLE_SERVICE_ACCOUNT_JSON`) or other credentials
  (`GEMINI_API_KEY`, `GMAIL_IMAP_PASSWORD`, `SESSION_SECRET`, `ADMIN_PASSWORD`, etc.) are
  set via `fly secrets set`, not `fly.toml` — Fly secrets are env-var-only, no file
  mounts, so anything expected as a file path locally is passed as inline content in
  production and handled accordingly by the reading code (see `sheets_sync.py`).

### Scheduler
- In-process `APScheduler` `BackgroundScheduler` (no external cron), started in the
  FastAPI lifespan and shut down on app exit.
- Sunday 18:00 Prague: weekly menu poll + Sheets sync.
- Mon–Fri at `ORDER_SUMMARY_SEND_TIME`: daily order summary email.

### Monitoring/Logging
- Fly.io built-in logs (`flyctl logs`), stdout from the app. No external alerting.

---

## 9. History / Notable Fixes

Kept briefly for context on why the app looks the way it does — not requirements, just
provenance for anyone reading the code later:

- **pdfplumber removed entirely.** It was the fallback PDF-text-extraction path used
  before the Gemini-based extractor existed, and its memory footprint (PDF rendering +
  concurrent Gemini calls) caused a production OOM kill on the 256 MB machine. Since
  Gemini extraction has been 100% reliable, the fallback was dropped rather than kept
  as dead weight — image size dropped ~92 MB → 75 MB, and 256 MB RAM was confirmed
  sufficient again afterward.
- **Gemini free-tier quota (20 req/day)** shapes several decisions in this document:
  merging calorie estimation into the PDF-extraction call (§6), and hardcoding the
  order-summary email copy instead of generating it (§ Email).
- **bcrypt via the raw `bcrypt` package**, not `passlib` — `passlib` is incompatible
  with `bcrypt>=4.1`.
- **Sticky topbar overlap bug**: `.topbar { top: 41px }` was copied verbatim from an
  earlier HTML mockup, where a dev-only sticky switcher bar sat above the real topbar
  and the offset compensated for it. The real app has no such bar, so the offset just
  left a gap that scrolled content (e.g. the admin panel's "Administrace" heading)
  visually collided with. Fixed to `top: 0`.
- **Wordmark rendering as "Ob ěd"**: `.wordmark`/`.login-mark` used a flex `gap` between
  a styled `<span>` and adjacent bare text; browsers wrap bare text in an anonymous flex
  item, so `gap` inserted unwanted space between them. Fixed by zeroing the gap.
- **Second OOM incident, and why prices briefly showed as 0 CZK (2026-08-12)**: hitting
  any admin write endpoint that calls `sync_all()` (Google Sheets sync) OOM-killed the
  256 MB machine partway through the Sheets client's import, dropping the HTTP response
  (502) *after* the DB write had already committed. A relative-adjustment endpoint
  (`price -= 50`, meant to backfill the non-soup discount onto already-stored rows) was
  retried several times against what looked like repeated failures, compounding the
  subtraction each time until every non-soup price hit 0. Fixed on three levels: (1)
  memory bumped back to 512 MB, (2) the endpoint replaced with an idempotent absolute
  setter (`POST /admin/menu/prices`, see §6) so retries can't compound, (3) `sync_all()`
  failures in admin endpoints are now caught and logged rather than allowed to make a
  successful write look like a failure to the caller (doesn't help against the process
  being OOM-killed outright, but does for ordinary Sheets API errors). No order data was
  affected — orders store a price snapshot at submission time
  (`Order.unit_price_czk`), and none were placed during the incident window.
