# Architecture through Phase 9

**Product authority:** [PROJECT_BRIEF.md](PROJECT_BRIEF.md). This describes the **implemented backend** plus its production boundaries. [openapi.yaml](openapi.yaml) is the generated, implemented HTTP contract; [openapi-roadmap.yaml](openapi-roadmap.yaml) retains the original `/api/v1` **future-route draft**. FastAPI currently implements `/api/*`, not that roadmap. The frontend includes the Phase 6 customer portal (see [CUSTOMER_PORTAL.md](CUSTOMER_PORTAL.md)), the staff workspace with human-reviewed AI assistance (see [STAFF_PORTAL.md](STAFF_PORTAL.md)), the Phase 7 scoped operations/admin console (see [PHASE_7.md](PHASE_7.md)), and and the Phase 9 first-party database-storage inventory (see [STORAGE_ADMIN.md](STORAGE_ADMIN.md)).

## Components and request flow

```text
CDN (immutable assets, public reference data only)
  |
Arabic-first React/Vite (browser) -- same-origin /api -- Nginx (limit_req, CSP/HSTS) -- FastAPI on Gunicorn/Uvicorn (stateless, HPA)
                                  |                         |-- PgBouncer (transaction) -- PostgreSQL 16
                                  |                         |-- Redis (limits, OTP, challenges, access cache)
                                  |                         `-- encrypted private binaries (attachments/exports/recordings) in PostgreSQL
                                  `-- Vite dev shell
Separate ARQ worker -- Redis cron -- restricted app_worker login -- PostgreSQL definer functions
                           `-- SMTP delivery (configured real provider only)/AV hook (unconfigured by default)
Admin-only migrator + synthetic seed ------------------------> PostgreSQL directly
Read-only analytics (optional DATABASE_READ_URL) -- replica PgBouncer -- streaming replica (same RLS)
Prometheus (/metrics, bearer) -> alert rules -> Alertmanager / Grafana; Sentry (PII scrubbed); JSON logs
```

The HTTP router → service → repository flow is in `backend/app/api/`, `app/services/`, `app/repositories/`; request/response constraints live in `app/domain/schemas.py`. The old Phase 1 SQL schema (`001`–`003`) is preserved; `004`–`006` add sessions, idempotency, upload reservations, notice outbox and SECURITY DEFINER commands. Phase 3 revisions `007`–`016` add staff workflows, automation, guarded administration, SLA calendars, scheduled reports, operations administration, analytics rollups and export formats; Phase 4 revision `017` adds encrypted integrations, queued actions, AI insights, API keys and voice/message delivery state. Phase 6 revisions `20261016_18`–`20261016_19` add customer profile/preference helpers and preference-aware notice delivery. Phase 7 revision `20261016_20` adds the staff-console team/assignment administration functions and permission catalog entries without duplicating the main branch's Phase 3/4 services. Phase 8 revisions `20261016_21` (PDPL consent evidence, privacy requests, retention, queue-depth telemetry) and `20261016_22` (statement-level InitPlan RLS for complaints) follow. Phase 9 revision `20261016_23` adds the `storage.manage` capability for the first-party, read-only storage inventory; revision `20261016_24` moves that inventory — and the bytes it describes — from the retired S3-compatible object store onto the owning rows in PostgreSQL (`attachments.content`, `export_jobs.content`, `call_logs.recording_content`), all encrypted with `APP_ENCRYPTION_KEY` before they reach the database. Revisions `20261017_25`–`20261017_27` then repair CSAT projection, the voice-consent
grant and the integration-job enqueue (an ambiguous PL/pgSQL `RETURNING id, state`
reference made every outbound integration enqueue fail). There are twenty-seven
ordered Alembic revisions, with `20261017_27` as head. The typed SQLAlchemy 2 mappings in `app/models/` mirror the database. Partitioned complaint keys retain `(id, created_at)` so child queries and events join the correct partition. Ticket numbers are globally unique and customer-facing; UUIDs are internal IDs.

### First-party database-storage administration

The built-in `/staff/storage` route is part of the existing staff SPA and uses
`GET /api/staff/storage` on the same origin. The API reads only the platform's
own tables (`attachments`, `export_jobs`, `call_logs`); list pages are bounded,
keyset-paginated and use opaque continuation tokens. The browser never receives
storage credentials, never sees ciphertext and never calls a storage service
directly — there is none to call.

The endpoint requires `storage.manage` and a global staff scope, checks those
conditions on every request, limits per-user inventory requests, sets
`Cache-Control: no-store`, and records successful inventory views in the audit
log. It returns object keys and operational metadata only. It is read-only by
design: no raw file body, write/delete, ACL, policy, or credential APIs are
exposed, so attachment scan verdicts, complaint scope, export ownership and
recording decryption remain enforced by their existing application routes.
This is a first-party product view, not a proxy to an external storage console.

### Authentication and authorization boundaries

1. Public `/api/auth/customer/otp/start` atomically rate-limits by mobile/IP, stores only a salted HMAC code in Redis with a five-minute TTL, and delivers it through the configured channel. No code or national ID appears in logs. OTP verification consumes a valid code once; it identifies or registers a customer using the encrypted-ID blind index. Guest tracking requires an exact ticket **and** national ID, rate-limits the pair/IP, and returns only public status/insurer/time metadata. Public insurer listing selects only non-sensitive columns.
2. Staff sign-in uses Argon2 and a narrow `staff_auth` SECURITY DEFINER lookup (no runtime SELECT on password hashes). Invite acceptance sets a password and activates the staff account. A seeded/forced password change is a restricted short-lived challenge, not a staff session. Optional TOTP enrollment and user/global enforcement issue only challenges until verification succeeds. Login, OTP and challenge attempts are limited in Redis.
3. A successful login issues a short-lived access JWT, a rotating refresh JWT and a readable random CSRF value as cookies. Access/refresh are HttpOnly and Secure; refresh is path-limited to `/api/auth`. Authenticated writes require a matching `X-CSRF-Token`. Each request parses the signed token, fetches **its own non-revoked session**, checks the **current token_version** in PostgreSQL and, for staff, loads a version-keyed Redis snapshot (five-minute TTL) or a fresh `access_snapshot()` when uncached. Version/session changes take effect immediately regardless of cache TTL; refresh replay revokes the whole token family. Logout/session management and privilege changes revoke sessions.
4. Every `/api/staff/*` HTTP route declares `require_permission("resource.action")`; `/api/me` exposes the effective staff snapshot. Permissions are unioned across roles with per-user overrides where **deny wins**. Access changes are SQL-atomic, audited and validate role grants, delegation rights and company/ownership scope; self-edits and removal of the last active administrator are blocked. Staff data queries receive an injected `AccessScope` (company, optional type, own/team/all) and **also** run under PostgreSQL RLS.
5. `backend/app/db.py` is the transaction entry point: an authenticated actor starts a transaction and installs `SET LOCAL app.user_id` / `app.scope` on the **same connection**. The runtime login is `app_runtime`, never owner/superuser/BYPASSRLS. Missing actor context yields no protected rows. Unauthenticated purpose transactions (`staff_auth`, `customer_auth`, `invitation`, `guest`) can call only narrow, scope-checked definer functions. The separate `app_worker` login cannot read base tables; its definer functions require both `session_user='app_worker'` and transaction-local `app.scope='worker'`. Migration credentials are not in API or worker containers. PgBouncer uses transaction pooling and asyncpg statement caching is disabled; never keep a session or GUC beyond a transaction.

### Complaints, files and notifications

- Complaint input validates national ID, mobile, active insurer/type support, policy-number format and lengths. Existing policies can be linked; **external policy ownership verification is not implemented**. Creation checks the authenticated customer's mobile/ID match, reserves a customer/key-specific 24-hour idempotency record, creates a unique ticket, event/audit and queued notice in one transaction; same-key/different-payload attempts conflict. Status updates, public replies, customer reopening (limited to a short post-resolution window) and CSAT are atomic commands with time/permission checks and audit/events.
- `ComplaintRepository.base_query` includes `AccessScope.predicate()` for list, count, counters, search and bounded export. Detail, timeline, messages, notifications, attachments and upload reservations use the **same scope plus partition key**; PostgreSQL RLS independently checks the actor's effective permission and company/type/ownership. Customer timelines exclude internal notes through RLS and response filtering. National IDs are AES-GCM ciphertext plus independent HMAC blind indexes; default detail shows only a masked suffix. Full reveal needs `complaints.view_full_national_id`, a reason and same-transaction audit. CSV output neutralizes spreadsheet-formula cells and is bounded/audited.
- The attachment service reserves at most five files/25 MB total (10 MB each) and issues a one-use five-minute signed upload token tied to actor, session, version and reservation. The proxied streaming upload enforces declared size, MIME sniffing, exact checksum and private storage; an upload finalizes as `pending`. A scanner must supply a `clean` verdict via the worker-only DB function before the repository can authorize a two-minute scoped signed download. Reauthorize on download, verify stored checksum and size, audit, and return `no-store`/`nosniff`. In this development stack the scanner is intentionally unconfigured; pending files are **not** downloadable.
- Customer creation, status, public reply and reopen notifications are queued transactionally. The worker claims rows with `SKIP LOCKED`, retry/backoff and status transitions. Customer and mobile-staff OTPs and notices use the configured real SMTP provider; no SMTP sink or SMS mailbox stub exists. Real SMS/WhatsApp and virus-scanner integrations are not supplied. Do not treat a queued or `pending_provider` notice as delivered.
- `/api/staff/complaints/stream` is a same-origin WebSocket with an access cookie; it checks permission and scope on connect and **again** on every refresh/heartbeat. It returns filtered snapshots, not a global event feed; revocation closes the socket. Rejects unrecognized messages/invalid filters. OpenAPI cannot describe the WebSocket; the generated spec includes `x-websockets`.


### Phase 3 controls

- Staff workflow uses explicit permission dependencies, `AccessScope` predicates and the same RLS transaction for listing/filter options, bulk actions, duplicates, notes, merges, saved views and notification receipts. Assignment rules are opt-in and admin-managed (`assignment_rules` + dry-run `preview_assignment`): the table-blind `app_worker` calls guarded SQL functions for round-robin, least-workload or by-company placement among agents whose *current* company/type/ownership scope covers the ticket, plus SLA warning/breach escalation and insurer-email notices.
- SLA rules carry an optional calendar (`time_zone`, up to 21 weekly windows, up to 60 holiday dates) and an alert lead time. `set_ticket_sla` resolves the rule in precedence order (company+type+priority, then progressively wider matches) and adds the target in *business* minutes, so a Friday-evening ticket is due on the next working window. Response and resolution clocks are separate: the first public staff reply stops the response clock while internal notes do not, `process_sla_warnings` marks at-risk tickets (event + assignee notification) before `process_sla_breaches` records misses and auto-transfers unresolved escalations. Calendars are validated twice — pydantic in the API and `valid_business_hours`/`save_sla_rule` in the database.
- Analytics derives all aggregates from the scoped complaint query and PostgreSQL RLS, requires both `analytics.view` and `complaints.view`, and a timezone-aware window of at most 366 days. Redis keys hash actor ID, token version, scope and filters (20-second TTL). `GET /api/staff/analytics/rollup` may read the `analytics_daily` materialized view through the definer `analytics_daily_scoped` reader, which re-applies company scope, insurance-type scope and fail-closed ownership per row; it is used only for whole-UTC-day windows without free-text search and only while the worker-refreshed view is fresh, otherwise the endpoint returns `source: "live"` from the ordinary scoped query. Nullable rollup dimensions use an all-zero UUID sentinel so the unique index (and a future `CONCURRENTLY` refresh) stays valid. Analytics exports are company-level only and omit subject, policy number and ticket number; CSV, Excel and PDF share one renderer.
- Admin role edits are guarded by SECURITY DEFINER functions: built-in roles cannot be edited, new grants must be held by the actor, affected staff versions/sessions are invalidated, and access-change records trigger recipient notifications. Forced logout, profile changes, email-based one-use password resets, reactivation, company/category/SLA/template edits, audit viewing and export all recheck permission and global/company scope in the database. Even a manager with `roles.manage` cannot grant what they do not possess.
- Async exports follow request → DB job → restricted worker `claim_exports` → `export_rows`. The worker function sets a *transaction-local* staff identity from the trusted job owner, checks current grants and per-row scope in a SECURITY DEFINER query, returns at most 1,001 rows, then restores the worker context. The worker fails jobs over 1,000 rows / 2 MB or without private storage, stores CSV with a per-attempt key and records an immutable `(id, created_at)` manifest. A download-only definer checks the current actor, grants, job TTL and **each** manifest row with `can_read_complaint_id` before releasing a private key, and audits the download. Access-change triggers immediately expire prior jobs; the cleanup cron removes private objects after expiry/revocation. No `export_job_rows` table privilege is granted to either runtime or worker login.
- `GET /api/me/notifications` exposes owned, RLS-filtered access-change notices even when `complaints.view` has been revoked. The complaint WebSocket still sends scoped list snapshots, *not* push notifications; a dedicated push channel remains deferred.
- Scheduled reports store a cron expression, IANA time zone, relative window and up to ten recipients. Every run is claimed by the worker (`claim_due_reports`), which *re-derives* the owner's current status, role permissions, deny overrides and scope rows before producing anything; a revoked owner deactivates the schedule and is notified instead of receiving rows. `report_rows` switches to the owner identity transaction-locally, filters with `can_read_complaint` per row and revives a relative window at run time, so a stale schedule cannot leak newer tickets. Three consecutive failures disable the schedule; runs are recorded in `report_runs` and announced through notifications/audit.
- Legacy Phase 3 integration settings keep only whitelisted non-secret values and vault references. Phase 4 provider credentials are stored separately as encrypted ciphertext; neither API nor worker receives direct table access.

### Phase 4 integrations, AI and voice

- `integration_jobs` is a durable SQL outbox/DLQ, not an in-memory ARQ payload queue. API handlers encrypt canonical job payloads with AES-GCM (purpose-bound AAD) and store only keyed idempotency/payload digests. The restricted worker claims due jobs with `SKIP LOCKED`, uses a bounded lease and at most five attempts by default, applies exponential retry delay, and preserves exhausted/permanent failures in `integration_dead_letters`. Admin replay is audited. Runtime/worker logins have no direct access to credential or queue tables; SECURITY DEFINER entry points authorize each operation.
- Enqueue functions require the relevant `integrations.send_email`, `integrations.send_whatsapp` or `integrations.ai_call` permission **and** current ticket scope. They bind the actor to the message/call/job. Worker claim rechecks staff state, permission, ticket access and current provider connection; WhatsApp opt-in and the same-ticket 24-hour customer-service window and voice consent are rechecked before provider calls. Customer-authored internal notes do not publish outbound message webhooks.
- Email adapters implement SMTP (also suitable for SMTP relays), SendGrid REST and SES raw-email delivery. Insurer forwarding verifies that every attachment belongs to the scoped ticket and is clean, then the worker verifies its storage checksum before sending. Agent customer replies use localized AR/EN ticket-numbered templates. IMAP and HMAC-authenticated normalized inbound email require an exact ticket number; HMAC delivery events update email delivery state. This inbound event contract is distinct from provider-specific webhook signatures unless an adapter is explicitly implemented.
- WhatsApp Cloud supports approved templates and free text only when the same ticket has a recent inbound customer message; customer availability/opt-in is checked both before enqueue and at worker claim. Meta delivery/read callbacks update the message row. The unified inbox returns portal, email, WhatsApp, phone-call and system timeline items under the same ticket scope.
- LLM input is minimized and masks Egyptian national IDs, phone numbers, email addresses, known customer names and policy references before leaving the service. Provider results are recursively minimized and encrypted at rest. Classification (including priority), sentiment, summary and suggested-reply insights remain `suggested` until a staff member with `integrations.ai_call` approves/rejects them; send endpoints additionally require the channel send permission and SQL verifies that the insight was accepted.
- Voice follow-up scheduling requires `integrations.ai_call`, ticket scope and recorded customer opt-in. Twilio/Vonage outbound adapters and a working mock provider share typed voice/STT/TTS/dialogue seams; normalized inbound events and call-status callbacks enter the durable queue. Transcripts are minimized and encrypted, recordings are separately AES-GCM encrypted before private object storage, and recording downloads require permission, ticket scope, audit, `no-store` and `nosniff`. The mock covers inbound consent/human fallback and outbound outcomes. A production inbound IVR/STT/TTS conversation flow still needs vendor-specific setup and end-to-end verification.
- Integration secrets are encrypted with `APP_ENCRYPTION_KEY`, which must be supplied to both API and worker. Admin list responses never return credential values; webhook signing secrets are shown once and stored encrypted. Public API keys store a one-way hash, validate the owner's live access and carry explicit permissions plus a company allow-list/global scope. Outgoing webhook requests use timestamped HMAC-SHA256 signatures and disable redirects; generic webhook endpoints are rate-limited and payload-bounded. Twilio's native form status callback verifies `X-Twilio-Signature` against the configured callback URL/auth token.
- Provider/worker tests use mocked HTTP transports and fake storage; no production provider was contacted. The Phase 4 migration is checked into the existing PostgreSQL testcontainers upgrade path. In this local environment Docker/PostgreSQL were unavailable: PostgreSQL and PL/pgSQL syntax parsers accepted the migration, but only CI/testcontainers can establish real database execution.

### Phase 8: hardening, scale, observability and delivery

* **Request pipeline.** The ASGI middleware runs outermost first:
  1. `RequestContextMiddleware`: request ID, JSON access log and metrics;
  2. `SecurityHeadersMiddleware`: CSP, HSTS, `nosniff`, framing, COOP/CORP,
     `no-store` for `/api`;
  3. `OriginGuardMiddleware`: CORS allowlist and cross-site write block;
  4. `BodySizeLimitMiddleware`: declared and streamed body limits, `413`;
  5. `RateLimitMiddleware`: Redis sliding windows per IP, user, endpoint
     class and API key, plus the abuse detector.

  Route-level guards add CAPTCHA (`captcha_guard`), lockout, CSRF,
  `require_permission`, and queue backpressure (`backpressure(...)` on AI,
  integration and export enqueue).
* **Client IP.** `X-Forwarded-For` is only honoured from
  `TRUSTED_PROXY_CIDRS` (`app/security/proxy.py`). Rate limits and audits use
  the resolved IP.
* **Keys.** `app/security/keyring.py` stores versioned AES-GCM ciphertext
  (`KR` + key id). Legacy ciphertext stays readable.
  `app.scripts.rotate_keys` re-encrypts online.
* **Privacy.** Migration 021 adds append-only `consent_records`,
  `privacy_requests` (30-day due date), the `record_consent`,
  `create_privacy_request` and `process_privacy_request` definers, worker-only
  `apply_retention`, and `queue_depths()` for backpressure and metrics. API:
  `app/api/privacy.py`. See [PRIVACY.md](PRIVACY.md).
* **RLS at scale.** Migration 022 rewrites `complaints_read` as InitPlans over
  `rls_*` helpers, so privileges are evaluated once per statement. See
  [ACCESS_CONTROL.md](ACCESS_CONTROL.md#rls-at-scale-phase-8-migration-022).
* **Runtime.** `backend/gunicorn.conf.py` and `app/gunicorn_worker.py`
  (Uvicorn worker without proxy-header trust or a server header). Readiness
  returns `503` while draining. Prometheus runs in multiprocess mode. Sentry
  is initialised only when `SENTRY_DSN` is set.
* **Observability.** `app/observability/{logging,metrics,sentry}.py`. Alert
  rules and the Grafana dashboard are in `infra/monitoring/`, mirrored into
  the Helm chart.
* **Kubernetes.** `deploy/helm/insurance-platform`: API, worker, web,
  PgBouncer, HPA, PDB, NetworkPolicies, Ingress, a migrate hook, a partitions
  CronJob, and monitoring CRDs.
* **Load and DR.** `loadtest/` (k6 and Locust, with
  `app.scripts.loadtest_sessions` fixtures); `scripts/backup/` (dump and
  restore drill). See [SCALABILITY.md](SCALABILITY.md) and
  [RUNBOOK.md](RUNBOOK.md).

## Delivery pipeline

- CI (`ci.yml`) gates six independent jobs:
  - **backend:** lint, format, migration lint, strict mypy and offline tests with coverage;
  - **frontend:** lint, typecheck, test, build and e2e;
  - **containers:** builds both images, runs `nginx -t` and a Trivy image scan, and proves the built API image still matches the committed contract;
  - **security:** pip-audit, npm audit and Trivy fs/secret/config;
  - **helm:** lint, template and kubeconform;
  - **postgres-rls:** migrates real PostgreSQL 16 through PgBouncer, runs the integration and pen-test suites plus a double seed, a k6 smoke and a backup/restore drill.
- CD (`cd.yml`) publishes immutable, SHA-tagged images to GHCR, then verifies the published artifacts by running them (SPA served by Nginx, API reachable through the same-origin proxy, contract parity) before any deployment is considered. The deploy job is gated on the `DEPLOY_ENABLED` variable, runs in a protected GitHub environment, validates the image owner/tag, ships only code (compose file, `infra/`, docs) over SSH, and passes values as `argv` so nothing is interpolated into a remote command string. On the host it pulls, migrates once, replaces `api`/`worker`/`web`, and only succeeds if `/api/health/ready` — which checks PostgreSQL through PgBouncer *and* Redis — turns green within 150 seconds.
- Production uses images, never building on the host (`docker-compose.prod.yml`), keeps the database on loopback with only the web port published, and takes its credentials from an on-host `.env.production`. Rollback is a tag change; schema rollback is not offered because the SQL revisions are forward-only.

## Data and operating model

- Date-range and `(created_at DESC, id DESC)` pages benefit from partitions/indexes; status/company/type/agent composite indexes and GIN `search_vector` cover common lists/search. The current API uses bounded offset pages, **not** distributed keyset cursor pages; ranking and performance of Arabic full-text search need measurement. Phase 8 adds the tuning, load-test harnesses and observability (see [SCALABILITY.md](SCALABILITY.md)). A 1M-user figure still requires running the k6 stress profile on production-sized infrastructure.
- Maintenance must provision the non-BYPASSRLS roles, apply Alembic migrations with a trusted BYPASSRLS migrator and run `app.scripts.partitions` monthly. Future partitions need controlled redistribution if DEFAULT already contains those dates. The synthetic seed is admin-only and opt-in; never run against production.
- Unit/API tests exercise validation, auth/CSRF, permission/scope injection, worker/AV fail-closed behavior and WebSocket reauthorization. The legacy integration suite probes direct PostgreSQL and PgBouncer RLS/transaction leakage. Additional Docker-dependent PostgreSQL 16 + Redis testcontainers cases exercise migrations, real API requests, refresh replay, scan gate, worker login and last-admin protection; they **skip locally when Docker is unavailable**, not substitute an emulator for real PostgreSQL. `make test` gates whole-`app` coverage at >80%.

### Subsequent work

Phase 6 adds the bilingual customer portal and staff workspace, a customer SSE stream (`app/services/realtime.py`, revalidating session/ownership per poll), migration `20261016_18` with SECURITY DEFINER customer-profile helpers, and migration `20261016_19` with notification-preference-aware delivery — details in [CUSTOMER_PORTAL.md](CUSTOMER_PORTAL.md) and [STAFF_PORTAL.md](STAFF_PORTAL.md). The canonical AI path is the Phase 4 asynchronous integrations flow in `backend/app/api/integrations.py` and `backend/app/services/integrations.py`; staff queue an analysis, the worker stores encrypted insights under the integration-job AAD/RLS boundary, and a human reviews before any suggested reply is sent.

Phase 7 adds a lazy-loaded staff console for scoped overview analytics and complaint queues, staff/role/access administration, operations configuration, teams and audit/report workflows. It deliberately reuses existing main-branch APIs: `/staff/complaints/{id}` remains the AI-capable `StaffTicketPage`; operations metadata uses the Phase 3 catalog/assignment/report APIs; encrypted provider credentials and AI continue through Phase 4. Migration `20261016_20` is additive and follows `20261016_19`. The console's `.staff-console` stylesheet is locally scoped, and its UI permission checks are only presentation — API permission gates, `AccessScope`, SECURITY DEFINER functions and PostgreSQL RLS remain authoritative. See [PHASE_7.md](PHASE_7.md) for screen-to-API boundaries and verification details.

Production SMS/WhatsApp rollout and live-provider verification, production antivirus, payment providers, dedicated push beyond WebSocket/SSE/polling, vendor-specific inbound IVR/STT/TTS, TLS certificates, an independent security review, Arabic full-text tuning, the PDPL legal review and production-scale capacity testing remain unfinished or unverified. Phase 8 supplies key rotation, automated restore drills, PDPL tooling, load-test harnesses and monitoring. The CD workflow publishes and smoke-tests images on `main`; deployment remains opt-in, and no successful production deployment is implied by CI. No production deployment or 1M concurrent-user claim follows from the current Compose setup. See the [ADRs](adr/) for access-control, PII, partitioning, pooling and Phase 3 SLA/report/rollup rationale.
