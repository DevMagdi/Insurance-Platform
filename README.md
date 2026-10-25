# Insurance Brokerage Complaint Management

Arabic-first insurance complaint platform. **[PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md) is the product source of truth.** This checkout implements Phases 1–9: the secure backend, SLA/reporting, integration and AI workflows, the bilingual customer portal, the scoped staff workspace, the operations and admin console, human-reviewed AI assistance, hardening and delivery, plus a first-party private-storage inventory.

Documentation:

* [Architecture](docs/ARCHITECTURE.md), [access control](docs/ACCESS_CONTROL.md), [ERD](docs/ERD.md)
* [Security (OWASP + pen tests)](docs/SECURITY.md), [privacy / PDPL](docs/PRIVACY.md)
* [Scalability and load tests](docs/SCALABILITY.md), [operations runbook and DR](docs/RUNBOOK.md), [deployment (Compose, Helm, CI/CD)](docs/DEPLOYMENT.md)
* [API guide](docs/API.md), [implemented HTTP OpenAPI](docs/openapi.yaml), [future-route roadmap](docs/openapi-roadmap.yaml)
* Portal and phase notes: [customer portal](docs/CUSTOMER_PORTAL.md), [staff](docs/STAFF_PORTAL.md), [built-in storage admin](docs/STORAGE_ADMIN.md), [Phase 7](docs/PHASE_7.md), [Phase 5 frontend](docs/PHASE5_FRONTEND.md)
* Service-desk/CRM spec mapping (multi-tenancy, Excel import, Client Folder, Sales CRM, issuing, policy lifecycle/commissions, Fawry/Paymob, Celery): [PHASE12.md](docs/PHASE12.md)
* The platform's single CRM (leads, customisable pipeline/Kanban, tasks & follow-ups, salesperson performance, Excel import — `crm_*` tables, `/api/crm`, `/staff/crm-module`): [CRM_MODULE.md](docs/CRM_MODULE.md)

The old `/api/v1/*` roadmap paths are **not** live routes.

## What's implemented

- **Authentication:** 5-minute single-use, five-attempt mobile OTP (Redis rate limits by mobile/IP); customer registration/identification; staff Argon2 sign-in with a common-password denylist, 48-hour emailed invitations and mandatory password change; optional TOTP enrollment and admin-required individual/global 2FA; EdDSA-signed JWT access and rotating refresh cookies with replay-family revocation; double-submit CSRF for authenticated writes, logout and session list/revoke.
- **Access:** `resource.action` permission guards on staff HTTP routes, role union plus deny-wins per-user overrides, company/type/ownership `AccessScope` on complaint repository reads **and** PostgreSQL RLS on the same `SET LOCAL` transaction. Versioned Redis snapshots with DB token-version/session rechecks, access-change audit, delegation limits, last-administrator protection and `/api/me` effective access. The WebSocket checks origin and repeats authentication, permission and scope on each snapshot. IDs are masked; staff reveal requires a dedicated permission, reason and audit entry.
- **Complaints:** Egyptian national-ID and mobile validation; strict input, supported insurer/type and policy-number validation; atomic 24-hour idempotency and unique tickets; scoped lists, full-text search, combinable filters, counters, detail, timeline, CSV export (staff), replies/internal notes, status changes, limited reopening and CSAT. Guest tracking requires **both** ticket and national ID and returns only public status metadata. Customer actions, staff changes, exports and ID reveals are audited.
- **Files & notices:** 5-file/25 MB upload plans, 10 MB per file, short-lived one-use upload tokens, MIME sniffing/size/checksum checks, **AES-256-GCM encryption into PostgreSQL** (`attachments.content`, envelope-bound to the row), pending virus-scan state and *clean-only* scoped downloads served from the database. Transactional creation/status/reply notifications go to an outbox; a separate, table-blind `app_worker` DB login processes delivery and scan hooks. Unconfigured scanners never mark a file clean; unconfigured outbound providers do not pretend delivery succeeded.
- **Foundation:** PostgreSQL 16 partitions and indexes, thirty-five ordered Alembic revisions (head `20261019_35`), a 35-key permission catalog, typed SQLAlchemy mappings, PgBouncer transaction pooling, Redis, Docker Compose with PostgreSQL and Redis (all email uses a configured real SMTP provider; private binaries live in the database, no object store), 5,000-ticket opt-in synthetic seed, CI/CD with published container images, and a React/Vite/Tailwind Arabic-first RTL shell. Implemented routes are under `/api/*`, not `/api/v1/*`.
- **Customer portal (Phase 6):** Mobile-first Arabic-first RTL + English, dark/light React portal: multi-step complaint wizard with live Egyptian national-ID/mobile validation and masking, WhatsApp toggle, insurer/type selection, drag-&-drop attachments (previews, XHR progress, 5-file/25 MB limits, remove/retry) uploaded via the signed plan flow after creation, privacy-preserving draft resume metadata (wizard step and stable idempotency key only; no form PII is persisted), inline OTP verification/registration for guests, and a success screen with the ticket number. My Tickets shows counters, search and status/company/date filters with pagination; ticket detail has the animated status timeline, assigned-agent display, public replies with attachments/downloads, reopen/CSAT actions whose eligibility mirrors the SQL windows, and live updates over a customer SSE endpoint that revalidates session/ownership every poll (30-second polling fallback). Guest tracking, OTP login, profile editing and email/WhatsApp/SMS notification preferences are included. Migration `20261016_18` adds SECURITY DEFINER profile helpers (no blanket customer UPDATE grant), `GET/PATCH /api/customer/profile`, notification read receipts and detail eligibility fields. See [CUSTOMER_PORTAL.md](docs/CUSTOMER_PORTAL.md).

**Phase 8: hardening, scalability, observability and delivery.**

* **Security:**
  * OWASP Top 10 audit with fixes ([SECURITY.md](docs/SECURITY.md));
  * strict CSP, HSTS and security headers on the API and Nginx;
  * CORS allowlist plus an origin guard;
  * double-submit CSRF everywhere;
  * Turnstile CAPTCHA on OTP, guest tracking, staff login and invites;
  * progressive brute-force lockout;
  * Redis sliding-window rate limits per IP, user, endpoint and API key, plus
    an abuse detector;
  * request body limits;
  * SSRF guard for outbound calls;
  * AES-GCM **key rotation** (`app.scripts.rotate_keys`);
  * PDPL consent evidence, data-subject requests, export and erasure, and
    scheduled retention ([PRIVACY.md](docs/PRIVACY.md)).
* **Access-control pen-test suite** (`tests/test_phase8_pentest.py`): IDOR,
  horizontal and vertical escalation, scope bypass via search, export, cache
  and attachments, self-escalation, stale permissions, enumeration, and RLS
  under 100 interleaved requests (20 in flight) through PgBouncer.
* **Scale:**
  * stateless API on Gunicorn with Uvicorn workers;
  * PgBouncer;
  * read-replica routing for analytics (`DATABASE_READ_URL`);
  * scope-aware Redis cache plus CDN cache headers;
  * queue backpressure;
  * graceful drain;
  * zero-downtime migration lint;
  * Kubernetes Helm chart with HPA, PDB and NetworkPolicies.

  Load tests (k6 and Locust) found that RLS ran **per row** (3.3 s for 5k
  rows). Migration `20261016_22` evaluates privileges once per statement
  (InitPlans): **7 ms**, staff list 2.8 s → 60 ms, with identical semantics
  proven by tests ([SCALABILITY.md](docs/SCALABILITY.md)).
* **Observability:**
  * JSON logs with request and user IDs;
  * Prometheus metrics (`/metrics`, token-protected);
  * Grafana dashboard;
  * Sentry with PII scrubbing;
  * liveness and readiness probes;
  * availability and security alert rules (`infra/monitoring/`) with
    runbook links ([RUNBOOK.md](docs/RUNBOOK.md)).
* **Delivery:**
  * CI adds migration lint, pip-audit, npm audit, Trivy fs and image scans,
    `nginx -t`, Helm lint, render and kubeconform, a k6 smoke against the real
    stack, and a backup/restore drill;
  * backup and restore scripts in `scripts/backup/`.

**Phase 3 + staff workspace:** Permission- and scope-filtered staff filters/search/workspace, assignment/reassignment, bulk status/assignment, escalation, merge/duplicate suggestions, saved views and private notifications are available in the React staff portal as well as the APIs. Rule-driven auto-assignment supports round-robin, least-workload and by-company strategies; the SLA engine honours calendars, alert lead times, response/resolution clocks, warnings, breach detection and auto-escalation. Analytics includes scoped KPIs, trends, heatmaps, scorecards, daily rollups and SLA/first-response metrics. Administration covers users/roles/scopes, companies/categories/SLA rules/templates, assignment rules, audit export and scheduled email reports. Audited async exports render CSV, Excel or PDF through the worker-only path with per-row scope reauthorization at download. See [STAFF_PORTAL.md](docs/STAFF_PORTAL.md).

**Phase 4 integrations + AI:** The SQL-backed integration queue is idempotent, retries with backoff, dead-letters exhausted jobs, rechecks sender permission and ticket scope at claim time, and encrypts credentials and payloads with AES-GCM. Email, WhatsApp, IMAP/webhook inbound channels, public API keys, outgoing webhooks and voice jobs share the durable queue. AI analysis minimizes PII before it leaves the service, encrypts resulting classification/sentiment/summary/reply insights, and requires recorded human approval before a suggested reply can be sent. The staff ticket screen queues analysis, polls the integration job, and reviews/sends only through these canonical Phase 4 endpoints. Migration `20261016_19` makes the customer notification worker honor email/WhatsApp/SMS preferences; notices with every channel disabled finish as `suppressed` instead of being delivered.

**Phase 5 frontend foundation:** generated OpenAPI TypeScript types/client, React Router/Zustand/i18n foundations, permission-aware components, access refresh helpers, PWA shell and the Phase 5 screens remain in the codebase; the Phase 6 customer and staff routes are the primary production workflow UI. See [PHASE5_FRONTEND.md](docs/PHASE5_FRONTEND.md).

**Phase 7 staff console:** the lazy-loaded, separately styled console adds permission/scope-driven overview and complaint queue screens, staff/access and role administration, teams and assignment rules, integration-reference settings, scheduled-report management, and filtered audit/access-change views and exports. It reuses the main Phase 3/4/6 APIs and database functions rather than duplicating them; complaint UUID routes still open the original AI-capable staff ticket page, customer/auth routes are unchanged, and the Phase 4 AI and encrypted integration-credential flows stay on their existing endpoints. Schedule execution, provider delivery and row-level permissions remain backend/worker responsibilities. Details: [PHASE_7.md](docs/PHASE_7.md).

**Custom administration dashboard:** `/staff/admin` is a first-party Arabic RTL dashboard built for this product (no imported admin template or hosted console). It uses the existing scoped analytics and directory APIs for KPIs, a hand-written SVG intake trend, case/company summaries, team access indicators and permission-filtered links. It requires global staff scope plus `analytics.view` and `complaints.view`; team data is fetched only with `users.view`. The regular staff overview remains at `/staff/overview`. See [STAFF_PORTAL.md](docs/STAFF_PORTAL.md).

**Phase 9 first-party storage admin:** `/staff/storage` is a product-native, Arabic RTL storage inventory of the platform's own PostgreSQL rows (attachments, generated export artifacts and call recordings). It supports logical folder navigation, bounded continuation pages, loaded-result search and per-row metadata inspection; it adds no external console dependency and never reads a bucket. `storage.manage` plus global administration scope is enforced by the API, and successful inventory views are audited. For insurance-data safety this console is read-only: contents, deletes and bucket policy changes stay outside it; files remain subject to existing complaint access and scan checks. See [STORAGE_ADMIN.md](docs/STORAGE_ADMIN.md).

**Still unfinished / production gates:** live-provider end-to-end verification and rollout, vendor-specific inbound IVR/STT/TTS, production antivirus, dedicated push beyond scoped WebSocket/SSE/polling, payment providers, deployment/observability/load and independent security reviews, real backup drills and PDPL review. Customer OTPs, invitations, and email notices are delivered through the configured real SMTP provider; mobile-browser staff OTPs use the globally configured Twilio SMS integration and its explicit destination-country allowlist. No local mailbox or sandbox fallback is supported. SMTP must use TLS and a verified public sender; Twilio SMS must have real credentials and a country allowlist verified by the operator. Never expose an unscanned attachment as clean. Capacity of 1,000,000 concurrent users is a **target**, not a measured capability of this Compose stack.

## Local development (Docker Compose v2)

1. `make init` creates a git-ignored mode-0600 `.env` with random development secrets (refuses to overwrite). For demo accounts set a **local-only**, 12+ character `DEMO_STAFF_PASSWORD` in `.env`; never commit the file.
2. `make up` builds and starts PostgreSQL, PgBouncer, Redis, migrations, API, worker, Vite and Nginx, then waits up to 180 seconds for the services to be ready. Configure `SMTP_HOST`, `SMTP_FROM`, port, credentials, and TLS options in `.env` to use a real mail provider; there is no development mail sink. The only stateful service is PostgreSQL, because attachments, export artifacts and call recordings are stored encrypted in the database. Open `http://localhost:8080/`; the first-party storage inventory lives at `/staff/storage` for authorized global administrators. API liveness `/api/health/live`, readiness `/api/health/ready`; interactive API docs are available only in non-production environments. Direct PostgreSQL: `localhost:5432`; PgBouncer: `localhost:6432` (bound only to localhost).
3. `make bootstrap-admin` optionally creates the **first administrator only** and is never started by `docker compose up`. It prompts for a name, e-mail and a 12+ character password (letters and digits), stores an Argon2id hash, grants the `administrator` role the full permission catalog and global all-companies/all-ownership scope, and inserts **no** companies, customers, complaints, teams or other demo data. It refuses to run if **any** staff account already exists, so it cannot add a second privileged identity by accident. Sign in at `/staff/login`; the API records the session and the login timestamp on first use. For automation set `BOOTSTRAP_ADMIN_NAME`, `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` and run `docker compose run --rm -T bootstrap_admin --non-interactive` (never commit or log a real password).
4. `make seed` is the alternative for demo environments: it inserts four insurers, roles and scoped demo staff, and ~5,000 tickets. It is idempotent. Demo staff must change their password at first login; the password-change challenge does **not** give access to protected staff endpoints. Never seed a production database, and do not mix `seed` with `bootstrap-admin` in the same database.
### Windows / PowerShell (no make)

Every `make` target has a plain equivalent; the full provider-setup and
connection-testing runbook in **[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)**
is written for PowerShell first. Bootstrap the first administrator without
make:

```powershell
docker compose run --rm -T bootstrap_admin                 # interactive
# or non-interactively (password via environment only, never committed):
$env:BOOTSTRAP_ADMIN_NAME='Platform Administrator'
$env:BOOTSTRAP_ADMIN_EMAIL='admin@example.com'
$env:BOOTSTRAP_ADMIN_PASSWORD='<12+ chars, letters and digits>'
docker compose run --rm -T bootstrap_admin --non-interactive
Remove-Item Env:BOOTSTRAP_ADMIN_PASSWORD
```

Then open `http://localhost:8080/staff/login` and configure real providers
under **Settings -> Integrations** (workplace settings: SMTP / Gmail OAuth /
Outlook Graph / WhatsApp Cloud / Twilio SMS / OpenAI / Twilio+Vonage voice /
ClamAV). Connection tests never send messages; sandbox sends require
explicit consent and only reach the configured sandbox recipient. Staff invitations
are different: they use the enabled **workplace email connection** and the invited
user’s actual address, never the sandbox recipient. Set `PUBLIC_APP_URL` to the
public SPA origin, choose a department in Users, then the recipient follows the
`/invite` link to create a password, complete personal details and upload a photo.
Click an employee photo in Users or the admin dashboard to open the full protected
profile. See [staff onboarding deployment checks](docs/DEPLOYMENT.md#staff-invitations-and-employee-profiles).

5. `make partitions` prepares current/upcoming UTC monthly partitions. `make down` retains data; `docker compose down -v` **destroys local data**. Changing `POSTGRES_DB` after the first boot requires a new *development* data volume (init scripts only run on an empty volume).

### Troubleshooting a `502 Bad Gateway`

A 502 on `/api/me` or `/api/public/config` means the web edge could not connect to the API; it is not a React/DevTools error. `make up` now waits for API readiness before returning, and the development Nginx proxy re-resolves Docker service names after containers are recreated. If the error remains, run these commands from the repository root:

```bash
docker compose ps -a
curl -i http://localhost:8080/api/health/live
curl -i http://localhost:8080/api/health/ready
docker compose logs --tail=150 api migrate pgbouncer postgres nginx
```

`/api/health/live` should return `200` with `{"status":"ok"}`. `/api/health/ready` should also return `200`; `503` there points to PostgreSQL/PgBouncer or Redis. If the API cannot start, inspect migration logs as well. Check the first failed service in `docker compose ps -a` and its logs. If you recreated `.env` after the database volume was initialized, PostgreSQL still has the previous role passwords; changing `APP_ENCRYPTION_KEY` also makes existing encrypted data unreadable. Restore the original `.env` if available. Do not run `docker compose down -v` unless you intentionally want to erase the local database and all its data.

`make init` creates the 32-byte `APP_ENCRYPTION_KEY`; keep it private and stable across API and worker containers because integration credentials, queued PII payloads, AI insights, transcripts and call recordings depend on it. Rotating it requires an explicit decrypt/re-encrypt procedure; simply replacing it makes existing ciphertext unreadable.

Use same-origin `/api/...` requests from the browser. A successful OTP verification or staff sign-in sets HttpOnly `access_token` (15 min by default), HttpOnly `refresh_token` (rotated via `POST /api/auth/refresh`), and a readable `csrf_token` cookie scoped to `/`; send `X-CSRF-Token` equal to that cookie on protected POST/PUT/PATCH/DELETE requests, including refresh. Existing API-scoped CSRF cookies are migrated automatically. Cookies are Secure by default; use HTTPS outside the browser's localhost development exception. **Never log or put tokens/OTPs/national IDs in URLs** (except single-use invitation/reset links in private email). Example customer flow: `POST /api/auth/customer/otp/start` with a verified email and mobile number, receive the code at that real email address, `POST /api/auth/customer/otp/verify`, then `POST /api/customer/complaints` with `Idempotency-Key`, CSRF header and a valid complaint body; `GET /api/customer/complaints` returns owned tickets. A nonlogged-in visitor can `POST /api/guest/track` with `ticket_no` and `national_id` only.

Some useful implemented routes (see [OpenAPI](docs/openapi.yaml) for request schemas):

| Area | Paths |
|---|---|
| Auth/self | `/api/auth/{customer/otp/*,staff/*,refresh,logout}`, `/api/me`, `/api/me/sessions`, `/api/me/2fa/enroll/*` |
| Customer | `/api/companies`, `/api/guest/track`, `/api/customer/complaints[/{complaint_id}]`, `.../timeline`, `.../reply`, `.../reopen`, `.../csat`, `/api/customer/notifications[/{id}/read]`, `/api/customer/profile`, `/api/customer/complaints/{id}/stream` (SSE) |
| Staff | `/api/staff/users`, `.../{id}/access`, `/api/staff/roles`, `/api/staff/settings/staff-2fa`, `/api/staff/complaints` (filters/search/counters/export/detail/timeline/reply/notes/status), `.../national-id:reveal`, `/api/staff/complaints/stream` (WebSocket) |
| Phase 3 | `/api/staff/analytics/{overview,scorecards,rollup}`, `/api/staff/views`, `/api/staff/complaints/{id}/{assign,reassign,escalate,merge,duplicates,sla}`, `/api/staff/complaints/bulk`, `/api/staff/exports[/{id}/download]`, `/api/staff/audit/{events,events.csv,events.xlsx,access-changes}`, `/api/staff/admin/{companies,categories,sla-rules,sla-rules/simulate,sla-rules/business-hours/preview,templates,integrations,assignment-rules,assignment-rules/preview,duplicates,duplicates/scan/{id}}` |
| Phase 4 | `/api/staff/complaints/{id}/{email,email/reply,whatsapp,ai/*,inbox,voice-followups,calls/*/recording}`, `/api/staff/admin/{integration-connections,webhooks,api-keys,integration-jobs/*}`, `/api/webhooks/{provider}/{company_id}`, `/api/public/v1/complaints/{ticket_no}`, `/api/customer/complaints/{id}/voice-consent` |
| Reports | `/api/staff/reports/schedules[/{id},/{id}/run,/{id}/runs]` (scheduled email reports) |
| Files | `/api/{customer,staff}/complaints/{id}/attachments/plan`, `/api/{customer,staff}/attachments/uploads/{id}/content`, `/api/{customer,staff}/attachments/{id}/authorize` and `.../content` |

Uploaded files remain **unavailable to download** until an integrated scanner returns `clean` via the restricted worker hook. The Compose worker intentionally has no scanner implementation, so local uploads stay pending unless an authorized scan verdict is recorded in a disposable test environment. Email invitations and OTPs require the configured real SMTP provider in every environment. A production rollout also needs HTTPS, a service-specific secrets store and independent security review.

## Local development (no Docker daemon)

Sandboxes, CI images and locked-down desktops often have no container runtime, which leaves the SPA behind a reverse proxy that answers *Bad Gateway*. `scripts/local_stack.py` starts the same services as the Compose stack from pinned wheels that embed the servers themselves — `pgserver` ships PostgreSQL, `redislite` ships Redis — and runs the API, the worker and Vite directly.

```bash
make init            # once: creates the git-ignored .env with local-only secrets
make local-install   # installs the 'localstack' extra (pgserver + redislite)
cd frontend && npm ci && cd ..
make local-up                  # postgres + redis + smtp + api + worker + vite
make local-bootstrap-admin     # migrations + the first administrator only (prompts)
# or: make local-seed          # migrations + demo dataset (needs DEMO_STAFF_PASSWORD in .env)
```

Open `http://localhost:5173/`; the API listens on `http://127.0.0.1:8000/` (`/api/health/ready` proves the database and Redis are reachable) and Vite proxies the browser's relative `/api` calls to it. `make local-status` lists the running services, `make local-down` stops them again, and logs, the database cluster and the captured emails live in the git-ignored `.local-stack/`.

Differences from the Compose stack, all development-only: no PgBouncer (the API connects straight to PostgreSQL) and no Nginx (Vite serves the SPA and proxies `/api`). Email delivery uses the real SMTP configuration from `.env`; no local sink or OTP logging is used. The demo staff still must change their password at first login.

## CI/CD

Every pull request and push runs `.github/workflows/ci.yml`, which has six jobs:

* **backend:** Ruff and format check (backend, scripts, loadtest), the zero-downtime migration lint, strict mypy, and offline pytest with an 80% coverage gate.
* **frontend:** lint, typecheck, Vitest, build and Playwright e2e.
* **containers:** builds the API and web images, runs `nginx -t`, scans them with Trivy (fails on fixable CRITICAL), boots them together, fails if the image contract drifts from `docs/openapi.yaml`, and renders the production compose file.
* **security:** pip-audit, `npm audit`, and Trivy fs/secret/config.
* **helm:** lint, template and kubeconform with default and production values, plus a monitoring-file drift check.
* **postgres-rls:** applies every Alembic revision to real PostgreSQL 16 behind PgBouncer, runs the RLS, integration and pen-test suites, seeds 5,000 tickets twice, runs a **k6 load smoke** with real sessions, and runs a **backup/restore drill** that re-verifies RLS on the restored copy.

`.github/workflows/cd.yml` runs on `main` and on `v*` tags: it publishes `ghcr.io/<owner>/insurance-platform-api` and `ghcr.io/<owner>/insurance-platform-web` tagged with the commit SHA, smoke-tests the **published** images end to end (SPA served, `/api/health/live` reachable through the web proxy, image contract equal to the committed OpenAPI), re-renders `docker-compose.prod.yml`, and only then optionally deploys over SSH — `pull` → `run --rm migrate` → `up -d` → readiness gate on `/api/health/ready`. Tag pushes additionally publish a GitHub Release. **Deployment is opt-in**: it needs the repository variable `DEPLOY_ENABLED=true` and the environment secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` (plus `DEPLOY_SSH_KNOWN_HOSTS` to pin the host key); when it is not enabled the job is skipped and the run summary says so. `make images`, `make prod-config`, `make deploy`, `make release VERSION=v1.2.3` wrap the same paths.

Production runs the pulled images via [`docker-compose.prod.yml`](docker-compose.prod.yml) — Postgres, PgBouncer (transaction pooling), Redis, migrations as a one-shot container, then api/worker/web with health checks, log rotation and exactly one published port. There is no object store to run, credential or back up separately: private binaries live encrypted in PostgreSQL. Credentials live only in an on-host `.env.production` (mode 0600); the API and worker containers never receive the admin URL or the Postgres superuser password. See the [deployment runbook](docs/DEPLOYMENT.md) for host preparation, rollback (a tag change) and backup drills.

## Tests and code quality

`make install` needs Python 3.11+ and Node 22+ and installs backend dev/test and frontend lockfile dependencies. `make ci` runs Ruff, strict mypy, backend pytest **with `--cov=app --cov-fail-under=80`**, ESLint, TypeScript, Vitest and the frontend build. Playwright e2e (API mocked at the network layer) runs in CI after installing Chromium: `cd frontend && npx playwright install chromium && npm run test:e2e`. `.venv/bin/pre-commit install` enables hooks. `PYTHONPATH=backend .venv/bin/python scripts/export_openapi.py` regenerates the implemented contract; the roadmap is intentionally separate.

`make test-integration` runs both legacy RLS/PgBouncer tests (requiring a **disposable, already-migrated PostgreSQL** and the three URLs below) and the PostgreSQL 16 + Redis **testcontainers** tests (requiring a Docker daemon). Testcontainers provisions its own disposable stack and migrates it; it skips, explicitly, when Docker is unavailable. The Phase 4 migration and queue/RLS assertions are included in that PostgreSQL suite. Do not point integration test URLs at production databases.

```text
TEST_DATABASE_ADMIN_URL=postgresql+psycopg://postgres:...@localhost:5432/insurance
TEST_DIRECT_DATABASE_URL=postgresql+asyncpg://app_runtime:...@localhost:5432/insurance
TEST_PGBOUNCER_DATABASE_URL=postgresql+asyncpg://app_runtime:...@localhost:6432/insurance
```

`cd frontend && npm run api:types` regenerates OpenAPI types from `docs/openapi.yaml`. The integrated Phase 1–8 tree is gated locally by Ruff/format, strict mypy, backend offline tests with the 80% coverage floor, frontend lint/typecheck/Vitest/build and Playwright discovery/execution in CI. Docker/PostgreSQL is exercised by the GitHub `postgres-rls` job, which applies every migration through PgBouncer, runs the RLS/integration suite and checks seed idempotence; the container job also proves the built API image matches `docs/openapi.yaml`. Check the PR and post-merge `main` CI/CD workflow status before claiming post-merge verification or deployment.

**Remaining frontend/production gates:** the Phase 5 landing demo form still needs a backend lead-capture route. Phase 7 provides an operations/admin console for teams, rules, integration references, reports and audit workflows; secure vendor credentials remain managed separately through the existing Phase 4 server-side integration boundary, and live provider behavior still needs verification. Accessibility review and production deployment checks remain external rollout work.

## Operations quick reference

| Task | Command |
|---|---|
| Backup / restore drill (compose) | `make backup`, `make restore-drill DUMP=/tmp/backups/<file>.dump` |
| Mint load-test sessions / k6 smoke | `make loadtest-sessions`, `make loadtest-smoke` (needs `RATE_LIMIT_MULTIPLIER` on a non-production API) |
| Helm checks | `make helm-lint` |
| Encryption key rotation | see [RUNBOOK.md](docs/RUNBOOK.md#encryption-key-rotation), then `python -m app.scripts.rotate_keys [--check]` |
| Migration lint | `make lint-migrations` |
| Metrics | `curl -H "Authorization: Bearer $METRICS_TOKEN" http://api:8000/metrics` |

## Security and scale boundaries

`app_runtime` is neither table owner nor BYPASSRLS, while `app_worker` has **no direct table privileges**. Authenticated transactions install `SET LOCAL app.user_id` / `app.scope`; queries also apply `AccessScope` so ID guessing, search filters, exports, child records, notifications and WebSockets inherit the caller's scope. National IDs are AES-GCM encrypted with separate HMAC blind indexes; default detail responses only contain a masked suffix. See the [access-control ADR](docs/adr/0002-access-control.md), [PII ADR](docs/adr/0004-pii-and-channels.md) and the [Phase 3 SLA/report/rollup ADR](docs/adr/0006-phase3-sla-reports-and-rollups.md). Phase 8 delivers key rotation, backup/restore drills, monitoring, alerting, load-test harnesses and PDPL tooling. Production still needs TLS termination, real provider integrations (SMS, AV scanner), an external penetration test, a PDPL legal review, and a capacity test on production-sized hardware (the k6 `stress`/`spike` profiles) before any 1M-user claim. See the production-readiness checklist in [SCALABILITY.md](docs/SCALABILITY.md#capacity-model) and [RUNBOOK.md](docs/RUNBOOK.md).
