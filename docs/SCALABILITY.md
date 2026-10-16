# Scalability, capacity and load testing (Phase 8)

**Goal from the brief:** about 1M concurrent users, mostly customers tracking
tickets, plus a smaller number of staff on scoped dashboards. This document
covers how the platform scales horizontally, what we measured, the bottleneck
we found and fixed, and what must be measured on production-sized hardware
before anyone claims a capacity figure.

> **Honest boundary.** Every number below comes from a 2-vCPU / 4 GB sandbox,
> where the load generator and the API shared the same CPUs. They show
> *relative* improvements and where the bottlenecks are. They do **not** show
> 1M-user capacity. That claim needs the `load`/`stress`/`spike` k6 profiles to
> run on the target cluster (see [Capacity model](#capacity-model)).

## Architecture for horizontal scale

| Layer | How it scales | Where |
|---|---|---|
| Edge / CDN | Hashed SPA assets are `immutable` for one year. `index.html` and the service worker are `no-cache`. Public reference data (`/api/companies`, `/api/public/config`) is `public, max-age=300`. Everything else under `/api/*` is `no-store`, so personal data never lands in a shared cache. | `infra/nginx/prod.conf`, `app/middleware/security_headers.py` |
| Nginx | Stateless, with per-IP `limit_req` zones (api 30 r/s, auth 1 r/s burst 10, upload 2 r/s). Body caps per route. HPA on CPU. | `infra/nginx/*`, Helm `web` |
| API | **Stateless.** Sessions are signed JWT cookies plus a DB session row. Rate limits, OTP, access snapshots and the analytics cache live in Redis. Gunicorn with Uvicorn workers; scaled by the HPA (12 to 400 pods in `values-production.yaml`). Pod disruption budget 75%. | `backend/gunicorn.conf.py`, `deploy/helm/...` |
| DB connections | The API holds **no in-process pool** (`NullPool`). PgBouncer in transaction mode multiplexes them (6 replicas, `max_client_conn` 20,000, `default_pool_size` 60). Statement caching is disabled for transaction pooling, and the RLS context is `SET LOCAL`, so it never outlives a transaction. | `app/db.py`, ADR 0005 |
| Read replica | `DATABASE_READ_URL` (asyncpg, `app_runtime`) sends the heavy analytics aggregates to a streaming replica. They run under the **same RLS actor context**, so scope is enforced there too. Production settings refuse a non-`app_runtime` read URL. | `app/api/analytics.py`, `app/settings.py` |
| PostgreSQL | Monthly range partitions on `created_at` (50 partitions plus DEFAULT), composite and GIN indexes, and a monthly partition CronJob. RLS predicates evaluated once per statement (migration 022, below). | `alembic/sql/001`, `022` |
| Redis | Sliding-window limiters (`ZSET`, one round trip per check), versioned access snapshots, and a scope-aware analytics cache. The cache key includes the actor's permission/scope fingerprint and token version, so two users with different scopes never share an entry. | `app/security/rate_limit.py`, `app/api/analytics.py` |
| Queue | Durable SQL outbox with `SKIP LOCKED`, plus an ARQ worker. **Backpressure:** when `queue_depths()` exceeds `QUEUE_BACKPRESSURE_THRESHOLD`, new AI, export and voice jobs get `503` with `Retry-After` instead of piling up. Exposed as `queue_depth{queue}` and `queue_backpressure_rejections_total`. | `app/services/backpressure.py` |
| Shutdown | Readiness returns `503` while draining. Gunicorn `graceful_timeout` 25 s, container `stop_grace_period` 35 s, and the Helm `preStop` sleep lets endpoints deregister before SIGTERM. | `app/main.py`, Helm `api.yaml` |
| Migrations | Expand/contract only. `app.scripts.lint_migrations` statically rejects blocking or breaking DDL in new revisions: `DROP TABLE/COLUMN`, renames, column type changes, `SET NOT NULL`, `ADD COLUMN ... NOT NULL` without a default, non-`CONCURRENTLY` indexes on existing tables, foreign keys without `NOT VALID`, `LOCK TABLE`, `TRUNCATE`, `VACUUM FULL`/`CLUSTER`. The Helm migrate hook sets `lock_timeout=5s`, so a blocked migration fails fast instead of queueing traffic. | `app/scripts/lint_migrations.py`, Helm `jobs.yaml` |

## Load tests

Two equivalent harnesses cover the three journeys the brief names: complaint
submission, tracking, and scoped dashboard analytics.

* **k6:** `loadtest/k6/platform.js` with profiles `smoke` (the CI gate),
  `load`, `stress` and `spike`. Thresholds: submission p95 < 800 ms, tracking
  p95 < 400 ms, dashboards p95 < 1200 ms, unexpected status < 1%.
* **Locust:** `loadtest/locust/locustfile.py`. User classes and weights:
  Customer 3, GuestTracker 4, StaffDashboard 1.

Fixtures come from `python -m app.scripts.loadtest_sessions`. It refuses to run
in production. It mints real sessions through `AuthService.issue_session` for
seeded customers and staff, so every request goes through the full auth, CSRF,
RLS and scope path. Run `make loadtest-sessions`, then `make loadtest-smoke`.
CI runs the smoke profile against the real API, PgBouncer and PostgreSQL in
the `postgres-rls` job.

The test harness raises `RATE_LIMIT_MULTIPLIER` (non-production only; production
refuses any value other than 1). Otherwise a single generator IP is correctly
throttled by the per-IP windows. Run separately with the multiplier at 1 to
verify the abuse controls.

## Bottleneck found: RLS evaluated per row

Run 1 (Locust, 150 users, 60 s) showed staff complaint lists with **p95 32 s,
max 50 s**. Unloaded, one admin page of 25 rows took **2.8 s**, consistently.
`log_min_duration_statement` pointed to the pagination count. `EXPLAIN ANALYZE`
as `app_runtime` with the admin RLS context showed:

```text
Aggregate  (actual rows=1)  Execution Time: 3288.8 ms   Buffers: shared hit=42087
  -> Append  (50 partitions + default)
       -> Seq Scan on complaints_2026xx
            Filter: app_security.can_read_complaint(company_id, insurance_type, ...)
```

`can_read_complaint()` is `SECURITY DEFINER`. PostgreSQL cannot inline it, so
it ran **once per row**, and each call ran about 8 catalog sub-queries
(permission, deny override, active staff, company, type, ownership, team): about
0.65 ms per row. The cost grows linearly with the table, which at millions of
complaints would make every scoped query unusable.

**Fix: migration `20261016_22` (`022_rls_scope_initplans.sql`).** New
`STABLE SECURITY DEFINER` helpers return the actor's privileges as values:

* `rls_complaint_mode()`: customer, global, scoped or none;
* `rls_scope_companies()`: `uuid[]`, where NULL means all;
* `rls_scope_types()`;
* `rls_scope_level()`;
* `rls_scope_teams()`.

The `complaints_read` policy wraps each helper in a scalar sub-select.
PostgreSQL runs these as **InitPlans**, once per statement, and the per-row
predicate becomes plain column comparisons (`company_id = ANY($1)`) that the
indexes and partition pruning can use. Semantics are unchanged:

* every staff actor in the seeded database sees exactly the same row count
  under the old and new predicate (admin 5,092, scoped agent 260, team lead
  2,435, revoked/disabled users 0);
* `test_initplan_rls_policy_matches_reference_predicate` asserts this
  equivalence for every fixture staff and customer, through the same suites that
  cover deny overrides, revocation, PgBouncer and 100 interleaved RLS requests (20 in flight);
* the test also checks that the plan contains `InitPlan` and never calls
  `can_read_complaint`.

`can_read_complaint()` is kept for child tables and for the reference test.

| Measurement (same data, same box) | Before 022 | After 022 | Change |
|---|---|---|---|
| `SELECT count(*) FROM complaints` as admin (EXPLAIN ANALYZE) | 3,288.8 ms | **7.4 ms** | ~445× |
| First page, 25 rows (EXPLAIN ANALYZE) | (included in list time) | **1.1 ms** | |
| Scoped agent count (260 visible) | ~3.3 s | **6.2 ms** | |
| `GET /api/staff/complaints?page_size=25` (unloaded, end to end) | 2.76–2.98 s | **53–64 ms** | ~50× |
| Analytics overview, cold (no Redis entry) | ~2.05 s | **65–165 ms** | ~15–30× |
| Analytics, cached (scope-aware Redis key) | 13 ms | 13–15 ms | |
| `POST /api/customer/complaints` (unloaded) | — | 83–95 ms | |
| `POST /api/guest/track` (unloaded) | — | 39–51 ms | |
| `GET /api/health/ready` | 120 ms | 10 ms | |

## Locust before/after (150 users, 60 s, same sandbox)

| Endpoint | Run 1 p50 / p95 | Run 2 p50 / p95 | Run 2 failures |
|---|---|---|---|
| `POST /api/guest/track` | 3.9 s / 5.2 s | 2.0 s / 4.0 s | 0 |
| `GET /api/customer/complaints` | 2.7 s / 3.4 s | 2.3 s / 3.2 s | 0 |
| `GET /api/customer/complaints/counters` | 2.5 s / 3.4 s | 2.2 s / 3.0 s | 0 |
| `POST /api/customer/complaints` | 6.1 s / 7.7 s | 5.3 s / 6.9 s | 0 |
| `GET /api/staff/complaints` | 3.9 s / **32 s** (max 50 s) | 1.8 s / **3.6 s** (max 4.0 s) | 0 |
| `GET /api/staff/analytics/*` | 56 × 422 (harness bug: no date window) | 1.9–2.2 s / 4.6–4.9 s | 0 |
| **Aggregate** | 29.2 rps, p95 5.5 s | **39.5 rps, p95 4.1 s, 0 failures** | |

Run 2 is still CPU-bound: load average 4.6 on 2 vCPUs, with Locust, two Gunicorn
workers, PostgreSQL and Redis on the same box. Unloaded latencies are 40–165 ms,
so the remaining queueing comes from shared CPU, not from a query or lock
problem. Throughput per API vCPU and per PgBouncer pool must be measured on
dedicated nodes.

Harness fix: the analytics endpoints reject open-ended ranges (`422`), so both
harnesses now send a bounded 30-day `date_from`/`date_to` window.

## Tuning applied

* **Gunicorn:** Uvicorn workers set to `min(8, 2×vCPU)` (`WEB_CONCURRENCY`).
  `max_requests` 20,000 with jitter, to limit memory creep. `keepalive` 75 s,
  above typical load-balancer idle timeouts. Request line/header limits.
  Workers use `/dev/shm` for the heartbeat file. Prometheus multiprocess
  directory is reset on boot.
* **PgBouncer:** transaction mode, `MAX_PREPARED_STATEMENTS=100`. The API's
  asyncpg statement cache is disabled. Pool sizes per replica come from Helm.
* **RLS:** InitPlan policy (above). Privilege checks are O(1) per statement
  instead of O(rows).
* **Caching:** analytics cached in Redis under scope fingerprints; reference
  data cached at the CDN/browser; immutable hashed assets.
* **Backpressure:** queue depth gates for AI, export and voice jobs.
* **Rate limiting:** Redis sliding windows per IP, user, endpoint and API key
  (see [SECURITY.md](SECURITY.md#rate-limiting-and-abuse-detection)).

## Capacity model

1M concurrent users is not 1M requests per second. With the journey mix above
(trackers poll about every 30 s through SSE/polling fallback, customers act
every few seconds while active, staff dashboards refresh every 2–5 s), expect
roughly **25k–40k rps at peak**, mostly cheap tracking reads.

Plan to measure, not assume:

1. Run `k6 ... -e PROFILE=stress` from distributed generators (k6 Cloud or k6
   operator) against a staging cluster with production values.
2. Record rps per API pod at p95 < 400 ms for tracking. Divide the peak rps by
   that number and add 40% headroom to set `minReplicas`/`maxReplicas`.
3. Size PgBouncer `default_pool_size × replicas` below the primary's
   `max_connections` minus maintenance headroom. Route analytics to replicas.
4. Watch `http_request_duration_seconds`, `queue_depth`, PgBouncer `cl_waiting`
   and PostgreSQL `pg_stat_statements` during the spike profile. Alerts are in
   `infra/monitoring/alerts.yaml`.
5. Repeat after each schema change that touches complaints; the
   `EXPLAIN (ANALYZE, BUFFERS)` snippets above are the regression baseline.

Known next steps at very large scale:

* replace offset pagination with keyset cursors for deep pages;
* use an approximate count for `total` beyond about 10k rows;
* move SSE fan-out to Redis pub/sub;
* add pre-aggregated partitions for multi-year analytics.
