# Authorization Review — On-Behalf Ordering

**Reviewed:** September 11, 2026
**Scope:** the authorization surface created by the order-modes run (waves 1–4, base `8ea4621`)
**Reviewer posture:** read-only. No application code was changed by this review.

The operator asked for one change with a security shape: *ordering on someone
else's behalf, previously admin-only, is now open to any logged-in user.* This
document records what that actually opened up, endpoint by endpoint, with
`file:line` references, and separates **accepted decisions** from **findings**.

> **Framing.** This is an internal office lunch app: not SOX-scoped, no
> payments, no PII beyond usernames. The operator's instruction was *"this is
> not a sox compliant software or anything - so just go ahead without concerns
> about security please."* Accordingly this review documents the accepted
> design rather than re-litigating it, and reports only departures from what
> was actually authorised.

---

## 1. Verdict

**No findings.** Every departure-from-intent this review looked for was
probed at runtime and came back clean:

| Check | Result |
| --- | --- |
| An endpoint that lost authentication entirely | None — see §3 |
| An `/admin/*` route that silently became public or user-reachable | None — all 16 still admin-only (§6) |
| Ordering rules enforced for self but bypassable on-behalf | Not bypassable — identical (§4) |
| On-behalf target being an arbitrary string rather than an existing user | Rejected with 404 before any write (§3) |
| A way to delete orders outside the intended replace-on-submit path | None — the only order-`DELETE` route is admin-only (§5) |

Two behaviours below are **accepted operator decisions, not findings**: the
silent overwrite (§5.1) and the admin-only → any-logged-in-user change itself
(§2).

---

## 2. The authorization change (intended, operator-requested)

`on_behalf_of` was added to the submit schema and honoured by `POST /orders`:

- `app/schemas.py:34` — `on_behalf_of: str | None = None`, with the comment
  at `app/schemas.py:31-33` recording that any logged-in user may set it and
  that it is deliberately not admin-only.
- `app/routers/orders.py:45-51` — resolves the target user; `None` means
  "order for myself".
- `app/routers/orders.py:36-38` — the docstring states the change and its date.

Introduced by a single commit, `21d95a2 "Allow any logged-in user to order on
another user's behalf"` — the change is explicit in the diff, not a side
effect, which is what the operator required.

**What this opened up, stated plainly:** any authenticated user can now write
order rows under any other registered user's account for any orderable date,
and (per §5.1) replace whatever that user had already chosen. Before this
change that capability existed only for admins via `POST /admin/orders`.

The historical claim that on-behalf submission was admin-gated was also
corrected in place rather than left to mislead — `app/routers/orders.py:120-126`
now says so explicitly, where it previously read *"Only admins can actually
submit on someone else's behalf."*

**Client call sites** (both post to `/orders`, not `/admin/orders`):
`app/templates/app.html:590-594` (oldschool grid) and
`app/templates/weekly.html:247-249` (guided prompt).

---

## 3. Endpoints accepting an on-behalf target, and their guards

Guard classification was read from each router object's resolved dependency
tree, not inferred from source.

| Endpoint | Guard | Existence check |
| --- | --- | --- |
| `POST /orders` (`app/routers/orders.py:28-33`) | `get_current_user` — `orders.py:32` | Yes — `orders.py:46-51`, 404 before any write |
| `GET /orders/week/{username}` (`orders.py:114-119`) | `get_current_user` — `orders.py:118` | Yes — `orders.py:127-129`, 404 |
| `POST /admin/orders` (`app/routers/admin.py:140-145`) | `require_admin` — `admin.py:144` | Yes — `admin.py:149-151`, 404 |

`get_current_user` (`app/auth.py:71-76`) raises 401 when no valid session
cookie resolves; `require_admin` (`app/auth.py:79-84`) adds a 403 for
non-admins. Neither was modified by this run.

**The target cannot be an arbitrary string.** `on_behalf_of` is resolved
against the `User` table and a miss is a hard 404 raised *before* the delete
and insert. Probed directly:

```
ARBITRARY-STRING on_behalf_of -> 404 {'detail': "User 'ghost_does_not_exist' not found"}
rows written by the 404 path: 0
```

Order rows therefore keep a real FK to a real user; no orphan or free-text
identity is reachable through this path. `Order.user_id` remains a hard FK and
no schema change was made.

**Anonymous callers cannot submit at all** — `POST /orders` returns 401
without a session, asserted by `tests/test_orders_on_behalf.py:132`.

**Related, unchanged:** `GET /users` (`app/routers/auth.py:12-17`) exposes the
username roster to every logged-in user. It is authenticated, pre-dates this
run, and is what backs the "Objednávám za" picker — noted for completeness,
not as a finding.

---

## 4. Ordering rules: self vs on-behalf

**Enforced identically.** There is exactly one rule function,
`_check_ordering_allowed` (`app/routers/orders.py:16-25`), and it is called at
`app/routers/orders.py:54` — *after* the target user is resolved but *before*
any menu lookup, delete, or insert. It takes only a date and has no knowledge
of who is ordering, so it cannot diverge between the two paths by construction.
The function is byte-identical to its pre-run version.

Probed both rules on both paths:

```
RULE PARITY (self vs on-behalf):
   past/on-behalf         (400, 'Cannot order for a past date')
   past/self              (400, 'Cannot order for a past date')
   weekend/on-behalf      (400, 'No ordering on weekends')
   weekend/self           (400, 'No ordering on weekends')
```

Zero rows were written by any of the four rejected submissions. This is also
pinned by the suite at `tests/test_orders_on_behalf.py:156` and `:170`.

The guided prompt mirrors the same rules client-side for UX
(`app/routers/pages.py:139`, `orderable`), but that is presentation only — the
server check above is authoritative and independent.

---

## 5. Can a user overwrite or delete another user's orders?

### 5.1 Overwrite — **ACCEPTED RISK, ALREADY DECIDED. Not a finding.**

`POST /orders` deletes every row the target user has for the date before
re-inserting:

- `app/routers/orders.py:74-76` — `db.query(Order).filter(Order.user_id == order_user.id, Order.order_date == target_date).delete()`
- `app/routers/orders.py:78-90` — re-insert under `order_user.id`

Combined with §2, **any logged-in user can silently replace a colleague's
existing order for a date.** There is no confirmation, no merge, and no record
of who submitted it.

**This was shown to the operator explicitly. They were offered
merge-instead-of-replace, a confirmation step, and an audit trail, and declined
all three** (small trusted office; it replaces a shared spreadsheet with the
same property). Per that decision this review adds no guards and files no
defect. It is documented here so the behaviour is known in advance rather than
discovered in production.

**What it means in practice:**

- Last submission wins; the previous choice is gone, not versioned.
- The overwritten user gets no notification. The only trace is the changed order.
- `Order` has no `submitted_by` column, so after the fact the rows are
  indistinguishable from ones the target placed themselves.
- The scope of one submit is exactly one `(user_id, order_date)` pair — it
  cannot reach another person's rows or another date.
- A submit with an **empty item list** returns `200` and clears the target's
  rows for that date without writing replacements. Probed:
  `target rows before=1 after=0`. This is the same accepted
  delete-then-reinsert path with nothing to re-insert, not a separate delete
  route — recorded here for completeness because the practical effect is
  "clear someone's order", which is worth knowing.

The behaviour is deliberately pinned by a test
(`tests/test_orders_on_behalf.py:83`, `test_on_behalf_submit_overwrites_target_existing_order`)
so it cannot be silently changed later in either direction.

### 5.2 Deletion outside the intended path — **none**

Every `.delete()` touching `Order` in the codebase:

| Location | Reachable by |
| --- | --- |
| `app/routers/orders.py:74-76` | any logged-in user — the accepted replace-on-submit path (§5.1) |
| `app/routers/admin.py:174` | admin only (`require_admin`, `admin.py:144`) |
| `app/routers/admin.py:203` (`DELETE /admin/orders/by-date`) | admin only (`require_admin`, `admin.py:199`) |

There is no non-admin route that deletes orders other than the accepted
replace-on-submit path. Probed with a plain user against the only order-`DELETE`
endpoint:

```
DELETE /admin/orders/by-date as plain user -> 403
   victim rows still present: 1
```

---

## 6. Did admin-only endpoints stay admin-only? — **Yes, all 16**

`app/routers/admin.py` and `app/auth.py` are **byte-unchanged by this run**
(`git diff b50cee1..HEAD` over both files is empty). `require_admin` appears 17
times in `admin.py` — once at import, once on each of the 16 endpoints.

Verified at runtime as a three-caller matrix over all 16 endpoints:

```
SUMMARY
  anonymous -> all 401 : True [401]
  non-admin -> all 403 : True [403]
  admin     -> no 401/403: True [200, 400, 502]
```

(The admin-row `400` is `POST /admin/users` on a duplicate username; the `502`s
are `parse-menu`/`send-order-telegram` reaching absent external services. Both
are past the authorization gate, which is what this check is about.)

`POST /admin/orders` was **not** loosened — it still requires an admin
(`admin.py:144`). The new capability was added alongside it on `POST /orders`
rather than by weakening the admin route. The order grid still routes an admin
editing via the pill row to `/admin/orders`, and everyone else to `/orders`
(`app/templates/app.html:590-591`).

Full guard map of the application (resolved from the dependency tree):

- **Unauthenticated by design:** `GET /health`, `GET /`, `POST /login`,
  `POST /logout`, `POST /telegram/webhook`.
- **Any logged-in user (`get_current_user`):** `POST /orders`,
  `GET /orders/my-week`, `GET /orders/week/{username}`, `GET /users`,
  `GET /menu/today`, `GET /menu/week`, `GET /dashboard`.
- **Session-optional page routes (`get_current_user_optional`, redirect to
  `/login` when anonymous):** `GET /login`, `GET /modes`, `GET /modes/weekly`,
  `GET /orders`.
- **Admin only (`require_admin`):** all 16 `/admin/*` endpoints.

`POST /telegram/webhook` (`app/routers/telegram.py:50-66`) is unauthenticated
but checks a shared secret header **when `telegram_webhook_secret` is
configured**; it defaults to `""` (`app/config.py:26`), in which case the check
is skipped. Pre-existing, untouched by this run, and outside this review's
scope — noted only so the "unauthenticated by design" list above is honest.

---

## 7. Residual risk — accept-or-fix

| # | Risk | Disposition |
| --- | --- | --- |
| 1 | Any logged-in user can silently overwrite/replace a colleague's order for a date; no confirmation, notification, or audit trail (§5.1) | **ACCEPTED — already decided by the operator 2026-09-11.** Guards were offered and declined. No action. |
| 2 | An empty-items on-behalf submit clears a target's order for a date without writing a replacement (§5.1) | **ACCEPTED** — same code path and same decision as #1. No action. |
| 3 | Order rows carry no `submitted_by`, so an on-behalf order is indistinguishable from a self-placed one after the fact (§5.1) | **ACCEPTED** — a `submitted_by` column was explicitly declined; no migration in scope. No action. |
| 4 | The username roster is readable by any logged-in user via `GET /users` (§3) | **ACCEPTED** — pre-existing, required by the picker, usernames only. No action. |
| 5 | `POST /admin/orders` checks weekends (`admin.py:153`) but not past dates, unlike `_check_ordering_allowed` | **PRE-EXISTING, admin-only, out of scope.** Confirmed present before this run (`git show b50cee1:app/routers/admin.py`). Not introduced or widened here. Fix only if admin backfill of past dates is ever unwanted. |
| 6 | `POST /telegram/webhook` skips its secret check when `TELEGRAM_WEBHOOK_SECRET` is unset (§6) | **PRE-EXISTING, out of scope.** Untouched by this run. Set the env var if the endpoint is ever a concern. |

Nothing in this table requires action to complete this run. Items 1–4 are
decided; 5 and 6 pre-date the run and were neither introduced nor widened by it.

---

## 8. How this was verified

- Guard classification resolved from each router's dependency tree at runtime,
  not read off decorators.
- Adversarial probes run against the app's real `TestClient` harness with the
  repo's own fixtures (temporary files, removed after running — no test files
  were added to the suite).
- Full suite: `.venv/bin/python -m pytest tests/ -q` → **95 passed**.
- Diff provenance: `git diff b50cee1..HEAD` confirming `app/routers/admin.py`,
  `app/auth.py`, `app/routers/auth.py`, `app/models.py` and `app/db.py` are
  unchanged by this run.
