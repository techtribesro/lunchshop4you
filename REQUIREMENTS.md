# Lunch Ordering Web App – Requirements

**Date:** August 11, 2026  
**Timezone:** CET/CEST (Prague)  
**Users:** 6–7 concurrent  
**Stack:** Python backend (Flask/FastAPI), Fly.io (free tier), Google Sheets (data store), internal scheduler

---

## Functional Requirements

### 1. Menu Ingestion

- System polls Gmail IMAP weekly for menu email from third party
- Parses email body to extract menu structure by day (Monday–Friday)
- Extracts: item name/description, price (CZK), day section
- Ignores allergen codes and category labels
- Stores menu for 5 days (Mon–Fri) with items grouped by category
  - Polévka (Soup)
  - Hlavní jídlo 1–3 (Main 1–3)
  - Vege. jídlo (Vegetarian)
- Overwrites previous week's menu on new parse
- Parsing must handle Czech text, multi-line descriptions, CZK prices

### 2. Ordering

- Users log in (username + password)
- View current day's menu (all categories visible)
- Select multiple items with quantities (qty × item)
- Submit before 11:30 AM CET daily
- Edit/resubmit allowed before cutoff (latest submission overwrites previous)
- System blocks orders after 11:30 AM CET
- No edit history stored

### 3. Dashboard

- Real-time: all users' current week orders (itemized by day/category)
- Weekly totals: per-user and aggregate spend (CZK)
- Monthly totals: per-user and aggregate spend (CZK)
- Visible to all logged-in users
- Updates immediately on order submit/edit

### 4. Authentication

- Login required (username + password)
- Manual password reset only (6–7 users)
- Session timeout: 24 hours idle

---

## Non-Functional Requirements

- **Users:** 6–7 concurrent
- **Hosting:** Free tier
- **Timezone:** CET/CEST (Prague local time)
- **Data Retention:** Current month + previous month
- **Performance:** Sub-second page loads acceptable for this scale
- **Uptime:** Best-effort (free tier SLA)

---

## Data Storage – Google Sheets

All data lives in a single Google Sheets document with tabs (sheets):

### Sheet: `users`
| username | password_hash | created_at |
|----------|---------------|-----------|
| yakob | hash_xxx | 2026-08-11 |
| user2 | hash_yyy | 2026-08-11 |

### Sheet: `menu`
| week_start | day | category | item_name | description | price_czk | parsed_at |
|------------|-----|----------|-----------|-------------|-----------|-----------|
| 2026-08-11 | Monday | Polévka | Chicken soup | ... | 25 | 2026-08-11T10:00:00Z |
| 2026-08-11 | Monday | Hlavní jídlo 1 | Pasta alla sicilia | ... | 185 | 2026-08-11T10:00:00Z |

### Sheet: `orders`
| user_id | order_date | item_name | quantity | unit_price_czk | submitted_at | week_start |
|---------|-----------|-----------|----------|----------------|--------------|-----------|
| yakob | 2026-08-11 | Chicken soup | 1 | 25 | 2026-08-11T11:00:00Z | 2026-08-11 |
| yakob | 2026-08-11 | Pasta alla sicilia | 1 | 185 | 2026-08-11T11:00:00Z | 2026-08-11 |
| user2 | 2026-08-11 | Chicken soup | 2 | 25 | 2026-08-11T11:15:00Z | 2026-08-11 |

### Sheet: `dashboard` (read-only, auto-computed)
| user | order_date | items_ordered | daily_total_czk | week_total_czk | month_total_czk |
|------|-----------|--------------|-----------------|----------------|-----------------|
| yakob | 2026-08-11 | 2 items | 210 | 210 | 210 |
| user2 | 2026-08-11 | 1 item | 50 | 50 | 50 |

**Access:**
- Backend uses Google Sheets API v4 (read/write authenticated via service account)
- Dashboard sheet auto-computed via SUMIF formulas or backend aggregation
- All data queryable directly from sheets for auditing/manual inspection

---

## Email Polling & Menu Parse

- **Frequency:** Once per week (day/time TBD)
- **Method:** Gmail IMAP polling from bot/service account
- **Trigger:** Internal Fly.io scheduler (Python `APScheduler` or similar)
- **Parsing:** Extract sections by day header (PONDĚLÍ, ÚTERÝ, etc.), then categories, then items
- **Output:** Upsert to `menu` sheet (replace previous week's data)
- **Error Handling:** Log parse errors; write failure status to sheets or stdout for Fly.io logs

## Google Sheets Implementation Notes

- **Service Account:** Create one in Google Cloud, grant access to Sheets document, download JSON key
- **Caching:** Cache menu in-memory after parse (reduces API calls during the week)
- **Concurrency:** Append-only for orders to avoid row conflicts. Menu is bulk-replaced weekly.
- **Manual fallback:** Users can view/edit sheets directly if app is down (sheets remain accessible)
- **Rate limiting:** Batch API calls where possible (e.g., batch append orders at end of day vs. per-order)

---

## API/Endpoints

### Authentication
- `POST /login` – username + password → session token
- `POST /logout` – destroy session

### Menu
- `GET /menu/today` – current day's menu with categories
- `GET /menu/week` – full week menu

### Orders
- `POST /orders` – submit/update order for today (before 11:30 AM CET)
- `GET /orders/my-week` – user's current week orders
- `GET /dashboard` – all users' week/month totals

### Admin (optional)
- `POST /admin/parse-menu` – manual trigger for email parse

---

## Constraints & Decisions

- **11:30 AM CET:** Hard cutoff. Orders submitted after are rejected.
- **No payment processing:** Prices for tracking only.
- **No email notifications:** Dashboard is source of truth.
- **Plain text parsing:** No HTML email structure assumed.
- **Single timezone:** All users in CET/CEST.
- **Google Sheets as DB:** No complex queries, but easy to audit and manual backup via Google Drive.
- **Rate limits:** Google Sheets API allows ~500 requests/100 seconds. Sufficient for this scale.
- **Data consistency:** Append-only for orders (no delete, only edit by resubmit). Menu overwrites weekly.

---

## Infrastructure

### Hosting: Fly.io (free tier)
- Python app runs in persistent container (always-on)
- Includes scheduler for cron jobs (weekly menu parse)
- 3 shared CPU, 256 MB RAM – sufficient for 6–7 users
- Storage: none needed (stateless, data in Sheets)

### Data: Google Sheets
- Authenticate via service account JSON key
- Read/write via `google-sheets-api` Python library
- Cost: free (part of Google Workspace or personal account)
- Audit trail: all changes visible in Sheets version history

### Email: Gmail IMAP
- Service account or bot account with read access to inbox
- Weekly poll: extract menu, parse, upsert to `menu` sheet
- No additional cost

### Session/Auth
- JWT tokens stored in cookies (stateless)
- Token validation on each request
- Refresh tokens optional (users can re-login after 24h)

### Monitoring/Logging
- Fly.io built-in logs (free)
- stdout from Python app
- Email alerts on parse failure (optional, via third-party service)

---

## Revision Note (2026-08-11)

The sections above are the original spec as provided. After discussion, the storage
architecture was revised — see design notes in project memory / commit history:
**SQLite is the operational datastore** (users, sessions, menu, orders); **Google
Sheets is a reporting/audit mirror**, synced asynchronously rather than queried live.
This replaces the "Google Sheets as DB" and "stateless JWT" sections above. Endpoints,
functional requirements, and the CZK/cutoff/retention rules are unchanged.
