# Access control

The decision record is in [ADR 0002](adr/0002-access-control.md). This page
describes the model as implemented through Phase 8, the layers that enforce it,
and the tests that prove it.

## Model

* **Permissions** are `resource.action` keys (34+ in the catalog). Roles grant
  sets of permissions. A per-user **deny overrides** everything; a per-user
  allow adds to the role union. Only `active` staff have any permission.
* **Data scope** for staff is the intersection of three grants. All three must
  be present; a missing company grant or ownership level denies everything:
  * **company**: specific insurers, or an explicit `all_companies`;
  * **insurance type**: optional rows; none means unrestricted;
  * **ownership level**: `own` (assigned to me), `team` (my teams), `all`.
* **Customers** see only rows where `customer_id` is their own, and only the
  public parts (no internal notes or staff-only fields).
* **Purposes** (`staff_auth`, `customer_auth`, `invitation`, `guest`, `webhook`)
  are unauthenticated transaction scopes. They can call only narrow,
  self-checking SECURITY DEFINER functions.

## Enforcement layers (defence in depth)

| Layer | What it enforces | Code |
|---|---|---|
| Edge | CSRF (double-submit), origin guard and CORS allowlist, rate limits, abuse block, CAPTCHA on public forms | `app/middleware/*`, `app/security/captcha.py` |
| Authentication | Signed JWT, plus a live session row, plus the current `token_version`. Revocation and privilege changes apply on the **next request**. | `app/api/deps.py`, `app/services/auth.py` |
| Route authorisation | Every `/api/staff/*` route declares `require_permission("resource.action")`. | `app/api/*` |
| Service and repository scope | `AccessScope.predicate()` on every list, count, search, counter, export and child lookup | `app/repositories/*` |
| Database | `app_runtime` is not the owner and not BYPASSRLS, and RLS is FORCEd. The actor context is `SET LOCAL app.user_id` / `app.scope` on the same transaction, so it is safe with PgBouncer transaction pooling. Child tables recheck the parent complaint. Writes go through guarded SECURITY DEFINER commands. | `alembic/sql/002`, `022`, `app/db.py` |
| Caches | Access snapshots are keyed by `token_version`. Analytics cache keys include the scope fingerprint. No personal data in shared HTTP caches (`no-store`). | `app/repositories/access.py`, `app/api/analytics.py` |
| Realtime | WebSocket and SSE re-run authentication, permission and scope on connect and on every refresh. Revocation closes the stream. | `app/api/websocket.py`, `app/services/realtime.py` |
| Files | Upload and download tokens are signed, short-lived and single-use, bound to actor, session and version. Scope is checked **before** the scan state, so the response does not leak whether a file exists. | `app/services/attachments.py` |

## RLS at scale (Phase 8, migration 022)

The `complaints_read` policy is a `CASE` over **InitPlans**:

```sql
CASE (SELECT app_security.rls_complaint_mode())       -- customer | global | scoped | none
  WHEN 'customer' THEN customer_id = (SELECT app_security.actor_id())
  WHEN 'global'   THEN true
  WHEN 'scoped'   THEN (company scope) AND (type scope) AND (own | team | all)
  ELSE false
END
```

The helper functions are `STABLE SECURITY DEFINER` with a fixed
`search_path`, and only `app_runtime` can execute them. PostgreSQL evaluates
each `(SELECT helper())` once per statement, so privilege checks are O(1) per
query instead of O(rows), and partition pruning and indexes still apply. The
reference function `can_read_complaint()` is unchanged and still used by child
tables. A dedicated integration test asserts that both predicates return the
same rows for every actor kind, and that the plan uses `InitPlan` without a
per-row function filter.

## Anti-escalation rules (access administration)

* Effective permissions and scope are computed from the database, never from
  submitted values.
* Nobody can grant a permission or scope they do not hold, or edit their own
  roles, scope or overrides.
* System roles cannot be modified, and the last active administrator cannot be
  removed or disabled.
* Each change is atomic, recorded in `access_change_log` with before and after
  values, and bumps the target's `token_version`. Sessions, caches and sockets
  pick up the change immediately.

## Verification

* `backend/tests/test_phase8_pentest.py`: IDOR, horizontal and vertical
  escalation, scope bypass (search, filters, export, analytics cache,
  attachments, children), self-escalation, stale permissions after
  revocation, enumeration and abuse blocking, lockout, CSRF/CORS/headers/413,
  privacy binding, 100 concurrent RLS requests, key rotation, and InitPlan
  policy equivalence.
* `backend/tests/test_rls_integration.py`: direct PostgreSQL **and** PgBouncer
  transaction-pooling isolation and GUC leakage.
* The `postgres-rls` CI job runs both on every push, then a k6 smoke test with
  real sessions and a backup/restore drill that re-verifies RLS on the
  restored copy.

Full matrix: [SECURITY.md](SECURITY.md#automated-access-control-penetration-suite).
