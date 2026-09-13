# Lunch Ordering Web App – Requirements

**Last updated:** September 11, 2026
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
- If the email has a **PDF attachment**, its text is extracted locally with
  **pdfplumber** (`app/services/menu_llm_extractor.py::_pdf_text`) — this is the **sole
  PDF-text path**, and it is position-aware, so the vendor's column/row layout survives
  instead of being flattened out of order.
- That extracted *text* (not the raw PDF) is then sent to **Groq**
  (`openai/gpt-oss-120b`, via the official `groq` SDK — `app/services/llm_client.py`,
  configured by `GROQ_API_KEY` / `GROQ_MODEL` in `app/config.py`). Groq returns, per
  item: day, category, name, description, price (CZK), and an **estimated calorie
  count** — extraction and calorie estimation happen in the **same API call**, not two
  (see §6).
- Groq exposes no `responseSchema` equivalent (Gemini, the previous provider, did), so
  the required JSON shape is spelled out in the prompt itself and the reply is parsed and
  validated on our side. A reply truncated at the output-token limit is raised as an
  explicit `LLMError` rather than half-parsed.
- If there's no PDF (rare, plain-text fallback), the body is parsed with a regex-based
  parser (`app/services/menu_parser.py`) that splits on the Czech day headers (PONDĚLÍ,
  ÚTERÝ, STŘEDA, ČTVRTEK, PÁTEK) and category labels, then calorie counts for that path
  are estimated with a **second, separate** LLM call (`calorie_estimator.py`) since
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

### 2.1 Mode chooser

After a successful login the user lands on **`/modes`** (`POST_LOGIN_PATH` in
`app/routers/pages.py`), a chooser between the two ways of ordering:

- **Mode 1 — Weekly order** (`/modes/weekly`): the guided prompt, §2.2.
- **Mode 2 — Oldschool order** (`/orders`): the existing order grid, unchanged.

The chooser is **deliberately not a one-time gate**. It is a plain `GET` that holds no
state and sets no "already chosen" flag, the order screen links back to it, and both
modes stay reachable at any time. Deep links to `/orders` and the existing redirect flow
still work; an unauthenticated hit on either mode redirects to `/login`.

### 2.2 Mode 1 — guided weekly prompt

A step-by-step flow that walks the loaded menu week one weekday at a time:

- Each day's items are presented as a simple **lettered (a/b/c…) choice**, with an
  optional **free-text note** per selection (same `Order.note`, 255 chars, as the grid).
- Per-day dates are computed **server-side** in `weekly_prompt_page` rather than derived
  in JS, and handed to the client as an `order_date` it posts verbatim. (The grid derives
  them client-side and carries a scar comment about `toISOString()` shifting the date
  back a day in UTC+ timezones; computing them once on the server keeps that bug from
  reappearing in a second place.)
- Each day carries an `orderable` flag mirroring `_check_ordering_allowed` (no past
  dates, no weekends) so non-orderable days are marked or skipped client-side instead of
  letting a submit come back `400`.
- A week with no menu at all drives a Czech empty state; a partially loaded week still
  gets the prompt, with the empty days marked.
- **After the last day**, the flow asks whether the user wants to order for someone
  else; if yes, it asks who (an existing registered user) and re-runs the same prompt for
  that person — see §2.3.

Each completed day persists through the existing `POST /orders` contract; the prompt
introduces no new write path.

### 2.3 Ordering on someone else's behalf

**Any logged-in user — not only admins — may submit an order for another _existing
registered_ user**, by setting `on_behalf_of` (a username) on `POST /orders`. An unknown
username is rejected with `404` before any write. This is available from **both** modes:
the guided prompt's end-of-week loop (§2.2) and a **dedicated button** in the oldschool
grid. In the grid, browsing the read-only "Objednávám za" pill row stays read-only —
submitting for someone else requires explicitly entering on-behalf mode via that button.

- **Authorization change, deliberate and reviewed.** This was previously admin-only, via
  `POST /admin/orders`. It was opened up on **2026-09-11** by explicit operator decision.
  **Rationale:** a small, trusted office; the app replaces a shared spreadsheet that had
  exactly the same property (anyone could edit anyone's row). The change is explicit in
  the code — `app/schemas.py` (`on_behalf_of`), and the `submit_order` docstring in
  `app/routers/orders.py` — rather than a silent side effect.
- **No schema change.** `Order.user_id` remains a hard FK and the
  unique `(user_id, order_date, item_name)` constraint is untouched. There is
  deliberately **no `submitted_by` column**, so after the fact an on-behalf row is
  indistinguishable from one the target placed themselves.
- **Ordering rules are not bypassed.** On-behalf submissions go through the same
  `_check_ordering_allowed` as self-service (no past dates, no weekends, cutoff). Only
  the admin path (`POST /admin/orders`, §5) bypasses the cutoff.

#### ACCEPTED RISK — overwrite on submit (operator decision, 2026-09-11)

This is a **documented, accepted decision, not a defect.**

`POST /orders` deletes every row the target user has for that date before re-inserting.
Consequently **any logged-in user can silently replace a colleague's existing order for
a date.** There is no confirmation, no merge, and no record of who submitted it; the
overwritten user is not notified, and the previous choice is gone rather than versioned.

The operator was shown this explicitly and was offered **merge-instead-of-replace, a
confirmation step, and an audit trail — and declined all three**, for the reason above.
Guards for this were therefore deliberately *not* built; do not add them back as a
"fix".

Related, recorded so it is not a surprise in production: a submit with an **empty item
list** on someone's behalf returns `200` and **clears that user's order for the date
entirely**, writing no replacement. It is the same accepted delete-then-reinsert path
with nothing to re-insert — not a separate delete route, and not separately guarded.

The blast radius of one submit is exactly one `(user_id, order_date)` pair; it cannot
reach another person's rows or another date.

**The full authorization surface review — every endpoint accepting an on-behalf target,
the admin-only endpoint audit, and the runtime probes behind these claims — is in
[`SECURITY_REVIEW_ON_BEHALF.md`](SECURITY_REVIEW_ON_BEHALF.md).** It is not duplicated
here.

### 2.4 The order grid (mode 2)

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
- Sourced from the extraction LLM (currently **Groq**, see §1) as part of the same
  PDF-extraction call — **not** a separate call. The single-call design originally
  existed to stay inside **Gemini's** free tier of 20 requests/day per project per model,
  back when Gemini was the provider; that constraint is also why the order-summary email
  copy is hardcoded rather than generated (see § Email and §9). The merged call was kept
  after the move to Groq — it is cheaper and one round-trip is less to go wrong.
- Values are best-effort LLM estimates, not authoritative nutrition data.
- **Manual backfill:** `POST /admin/menu/calories` lets an admin set `calories_kcal` for
  specific (day, item_name) rows in the current week directly, bypassing the LLM
  entirely. This exists for cases like a menu parsed before the kcal feature existed (so
  every row is `null`), or when rough hand-entered estimates are good enough for the day
  — no LLM call spent, and the next scheduled/triggered re-parse overwrites these with
  real model estimates anyway.
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
computed `dashboard` tab to a Google Sheet after every menu parse and every order
write (user submits/edits an order, or an admin assigns/clears one), using the shared
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
- `GET /login` — login form; a successful login lands on `/modes` (§2.1), not `/orders`
- `GET /modes` — the mode chooser (§2.1). Redirects to `/login` if not authenticated.
  Re-enterable at any time; it is not a one-time gate
- `GET /modes/weekly` — mode 1, the guided weekly prompt (§2.2); serves the page plus the
  serialized week (per-day dates and an `orderable` flag) the client steps through
- `GET /orders` — mode 2, main app page (menu, cart, dashboard, admin panel if `is_admin`)

### Auth
- `POST /login` — username + password → session cookie
- `POST /logout` — destroy session

### Menu
- `GET /menu/today` — current day's menu
- `GET /menu/week` — full week menu

### Orders
- `POST /orders` — submit/replace an order for a given date (today, or an
  early-opened date). Accepts an optional **`on_behalf_of`** username to submit for
  another existing registered user — available to **any logged-in user**, not just
  admins (§2.3); unknown username → `404`. Replaces the target's rows for that date
- `GET /orders/my-week` — caller's current-week orders
- `GET /orders/week/{username}` — any logged-in user may *view* any user's current-week
  orders (backs the "Objednávám za" pill row and the on-behalf flows)

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
  (`GROQ_API_KEY`, `GMAIL_IMAP_PASSWORD`, `SESSION_SECRET`, `ADMIN_PASSWORD`, etc.) are
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

## 8. Known Issues (open)

Current, unresolved behaviour — distinct from §9, which is history. If you are reading
this after a fix has landed, **check the code before trusting this section**; it
describes the state at the time of writing (September 11, 2026).

*No open issues at present. The two weekend week-resolution bugs described below were both
fixed before or shortly after this document was merged; they are retained here because the
reasoning is worth keeping and because they are the same defect in two places.*

### FIXED 2026-09-11 — weekend submissions were invisible to the reader endpoints

**This is resolved.** `my_week_orders` and `user_week_orders` now filter on
`menu_target_week_start()` rather than a bare `week_start()`, so the read side matches
the week the write side actually stores. On a weekday the two helpers agree, so the
change is a no-op Mon–Fri. A regression test (`tests/test_weekend_read_week.py`) fails
against the old code and passes against the new. The original description follows, for
the record:

- **Weekend submissions were invisible to the reader endpoints.** Order *writes* and order
  *reads* disagree about which week they mean when the request happens on a Saturday or
  Sunday:
  - `POST /orders` stores rows under `week_start(target_date)` — derived from the order's
    own date, which on a weekend is the **upcoming** week.
  - `GET /orders/my-week` and `GET /orders/week/{username}` both filter on a bare
    `week_start()` — which resolves to the Monday of the **current** week (on a Sunday,
    the week that is ending that day, not the one about to start).

  The practical effect: a user who places an order over the weekend **cannot see the
  order they just placed** — the write succeeds and the rows exist, but the reads look
  under the wrong Monday. Weekday use is unaffected, since both sides then resolve to the
  same week.

  The two reader call sites are the `week_start()` filters in `my_week_orders` and
  `user_week_orders` in `app/routers/orders.py` (at the time of writing, lines 108 and
  132). Compare `app/timezone.py::week_start` with `menu_target_week_start`, which
  already encodes the "a weekend call means the upcoming week" rule for menu refreshes
  and is the precedent for how to resolve this.

  Tracked as **t16**, which landed before this document was merged. The fix is in
  `app/routers/orders.py` (`my_week_orders` and `user_week_orders`), and
  `menu_target_week_start` — cited above as the precedent — is exactly what it adopted.

### FIXED 2026-09-13 — the page routes rendered a different week than ordering accepted

**This is resolved.** It is the *same class of divergence* as the 2026-09-11 fix above,
on the other side of the app: that one reconciled the order **reader** endpoints with the
order **writer**, and this one reconciles the **page** routes with both. Read and write
must agree about which Monday is in play; wherever they were derived from different
helpers, a weekend pulled them apart. Two instances of one family of bug, not two
coincidences — if a third `week_start()` shows up on a request path, treat it as the same
defect until proven otherwise.

- **The page routes rendered the outgoing week while ordering accepted the upcoming one.**
  `app/routers/pages.py` computed `ws = week_start(today)` in both the weekly prompt and
  the order grid (at the time of writing, lines 125 and 171) and filtered
  `MenuItem.week_start == ws`, while `app/routers/orders.py` targets
  `menu_target_week_start()`. On a Saturday or Sunday those resolve to **different
  Mondays**.

  The practical effect, reproduced end to end on production-shaped data (Sunday
  2026-09-13, the database holding only week 2026-09-07): the page rendered week
  2026-09-07 — five weekdays, each with dishes — and **every one of them came back
  `orderable=False`**, while `POST /orders` for a date the page had just displayed
  returned `400 "Cannot order for a past date"`. A full menu rendered and nothing in it
  could be selected. Reported by the operator as *"i cant chose anything from the menu
  which is whay i could"*. This was a **code defect, not missing data** — the menu rows
  were present and correct the whole time.

  The fix: both page call sites now use `menu_target_week_start()`, the same helper and
  the same reasoning the order side adopted, so what a user sees and what a user can order
  agree **by construction** rather than by coincidence. `pages.py:189` filters the user's
  own saved `Order` rows by that same single `ws` variable, so the order filter moved with
  the displayed week automatically and a user's saved orders for the orderable week still
  render. On a weekday the two helpers resolve identically, so the change is a provable
  no-op Mon–Fri.

  A regression test (`tests/test_weekend_page_week.py`) pins both a Saturday and a Sunday
  and asserts that the week the page renders equals the week ordering accepts, and that a
  dish shown as orderable can actually be ordered end to end. It fails against the old
  code and passes against the new.
  `tests/test_weekly_prompt.py::TestWeeklyPayloadShape::test_each_day_carries_its_iso_date`
  was **updated** rather than skipped, because it had encoded the old rule directly.

  Deliberately **not** changed by this fix: `menu_target_week_start()` itself, the
  scheduler cadence, and `_check_ordering_allowed`.

**Still deferred (not cancelled).** Two improvements were the original plan for this work
and were superseded once the divergence above turned out to be the actual cause of the
operator's report. They do not fix this symptom, but they remain worth doing for
**genuinely empty** weeks, where the current copy states only that no menu is loaded:

- Self-explanatory empty-state copy that says *why* the menu is empty and *when* it is
  expected, so a normal waiting state does not read as breakage.
- An admin-only "pull next week's menu now" affordance. The endpoint already exists —
  `POST /admin/parse-menu?for_next_week=true` (§5) — so this is a UI affordance over
  existing behaviour, not new ingestion logic.

## 9. History / Notable Fixes

Kept briefly for context on why the app looks the way it does — not requirements, just
provenance for anyone reading the code later:

- **pdfplumber: dropped once, and now back as the only PDF-text path.** An earlier
  revision of this document stated "pdfplumber removed entirely"; **that is no longer
  true and should not be relied on.** pdfplumber was originally a *fallback* text path
  alongside an extractor that sent the whole PDF to the model, and it was removed after
  its memory footprint (PDF rendering + concurrent LLM calls) contributed to a production
  OOM kill on the 256 MB machine. The current design is the other way round: pdfplumber
  extracts the text **locally and always** (`menu_llm_extractor.py::_pdf_text`) and only
  that text goes to the LLM — which is both cheaper and more reliable on this vendor's
  column layout. The machine is back at 512 MB (see the second OOM incident below), so
  the original memory argument no longer applies.
- **Provider moved from Gemini to Groq.** The extractor now calls Groq
  (`openai/gpt-oss-120b`); see §1. Gemini references that remain in this section are
  **history**, retained because the Gemini free-tier quota is the actual reason several
  still-current design choices look the way they do.
- **Gemini free-tier quota (20 req/day)** — a *historical* constraint, from when Gemini
  was the provider — shapes several decisions in this document: merging calorie
  estimation into the PDF-extraction call (§6), and hardcoding the order-summary email
  copy instead of generating it (§ Email). Both were kept after the provider change on
  their own merits.
- **Brand palette: navy + turquoise, not green.** When the golfshop4you re-theme was
  specified the operator first answered that "green from the logo is enough", believing
  the real palette could not be extracted from the shop. It could: the verified identity
  is **navy (`#0B1F3A`) + turquoise (`#0FA9B8`)** with **no green** in it, taken from the
  live site's own CSS custom properties. The operator was re-asked and confirmed the real
  tokens. Recorded because the earlier "green" instruction is still in the run history
  and is superseded.
- **Two weekend bugs, one root cause: `week_start()` on a request path.** The weekend
  read bug (2026-09-11, order endpoints) and the page/order divergence (2026-09-13, page
  routes) were found and fixed a fortnight apart and looked like unrelated symptoms — one
  hid orders the user had just placed, the other rendered a menu none of which could be
  selected. They are the same defect: a bare `week_start()` means "the week containing
  today", which on a Saturday or Sunday is the week that is *ending*, while every path
  that actually accepts an order means the week that is *starting*
  (`menu_target_week_start()`). Mixing the two on one request path is only invisible
  Mon–Fri, when they agree. Both fixes were the same one-line-per-call-site move to
  `menu_target_week_start()`. See §8 for both write-ups.
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
