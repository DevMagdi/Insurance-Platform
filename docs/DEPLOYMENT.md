# Deployment runbook (CI/CD)

Two workflows own the lifecycle:

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | every push and pull request | Ruff, ruff-format and strict mypy; migration lint; offline pytest with an 80% coverage gate; frontend lint/typecheck/Vitest/build and Playwright e2e. **Container build** of both images with an `nginx -t` check, a Trivy image scan (fails on fixable CRITICAL) and a smoke test, plus a `docker compose config` render of the production file. **Security** job: pip-audit, `npm audit`, Trivy fs/secret/config. **Helm** job: lint, render and kubeconform. Then the real PostgreSQL 16 + PgBouncer RLS/pen-test/migration/seed suite, a **k6 load smoke** and a **backup/restore drill**. |
| `.github/workflows/cd.yml` | push to `main`, tag `v*`, manual dispatch | builds and publishes the images to GHCR, smoke-tests the **published** images end to end (SPA served, `/api/health/live` reachable through the web proxy, image contract equals `docs/openapi.yaml`), re-renders the production compose file, optionally deploys over SSH, and publishes a GitHub Release for tags |

Nothing deploys by accident: the `deploy` job runs only when the repository
variable `DEPLOY_ENABLED` is `true` (push to `main`) or when a manual dispatch
sets `deploy: true`. When it is skipped the run summary says so explicitly.

## Enabling deployment

1. **Repository variable** `DEPLOY_ENABLED` → `true` (Settings → Variables → Actions).
2. **Environment secrets** on the `staging` (and optionally `production`) environment:
   * `DEPLOY_HOST` — hostname of the deployment host,
   * `DEPLOY_USER` — SSH user with Docker permissions,
   * `DEPLOY_SSH_KEY` — private key for that user (never a password),
   * `DEPLOY_SSH_KNOWN_HOSTS` *(recommended)* — the pinned `known_hosts` line; without it the host key is accepted on first use.
3. **Repository variables** (optional): `DEPLOY_PATH` (default `/srv/insurance-platform`), `HTTP_PORT` (default `8080`).
4. Protect the `production` environment with required reviewers if a human gate is wanted.

## Preparing the host (once)

```bash
# Docker Engine 24+ with the compose plugin, plus curl, ssh, tar and rsync.
sudo mkdir -p /srv/insurance-platform
sudo chown "$USER" /srv/insurance-platform
```

Create `/srv/insurance-platform/.env.production` yourself (mode `0600`, never in
Git, never in a workflow log). Every variable the stack consumes is documented in
`.env.example`; the production-only additions are below. `IMAGE_OWNER` (the
lowercase GHCR namespace, e.g. `devmagdi`) and `IMAGE_TAG` (the commit SHA or
release tag of the image to run) are exported by the pipeline per deploy; put your
own values in the file only when running the stack by hand.

```text
ENVIRONMENT=production
PUBLIC_APP_URL=https://app.example.com
COOKIE_SECURE=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=no-reply@example.com
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_START_TLS=true
SMTP_USE_TLS=false
HTTP_PORT=8080
APP_WORKER_DB_PASSWORD=...
# Phase 8: required in production (the API refuses to boot without them)
TURNSTILE_SITE_KEY=...              # Cloudflare Turnstile widget key for public forms and OTP
TURNSTILE_SECRET_KEY=...
METRICS_TOKEN=...                   # >= 32 chars; required in production; Prometheus uses "Bearer <token>"
JWT_KEY_ID=active
JWT_PRIVATE_KEY_B64=...              # Ed25519 private PEM encoded as URL-safe base64
JWT_PUBLIC_KEY_B64=...               # matching Ed25519 public PEM encoded as URL-safe base64
JWT_PREVIOUS_PUBLIC_KEYS=            # optional comma-separated kid:base64url-PEM during key rotation
# Phase 8: optional
CORS_ALLOWED_ORIGINS=               # comma-separated https origins; empty = same-origin only (default SPA)
SENTRY_DSN=                         # error tracking; PII is scrubbed before sending
WEB_CONCURRENCY=4                   # Gunicorn Uvicorn workers per API container (default 2 x vCPU, max 8)
APP_ENCRYPTION_KEY_ID=1             # bump on rotation (docs/RUNBOOK.md#encryption-key-rotation)
APP_ENCRYPTION_PREVIOUS_KEYS=       # "<id>:<old key>,..." while re-encrypting
APP_IDEMPOTENCY_KEY=                # set to the ORIGINAL encryption key before the first rotation
```

Private binary storage: nothing to configure. Attachments, generated export
artifacts and call recordings are encrypted with `APP_ENCRYPTION_KEY` and stored
in columns on their owning rows (`attachments.content`, `export_jobs.content`,
`call_logs.recording_content`), so production runs PostgreSQL, PgBouncer, Redis
and the app containers and nothing else. There is no bucket, endpoint, access
key, IAM policy or storage volume to provision, rotate or back up separately —
and therefore no second copy of customer documents to protect.

**Upgrading an installation that used the retired object store:** before
deploying revision 24, the previous release is still running and still uses the
object store. Deploy the new release as usual; rows created *before* revision 24
keep `content IS NULL` and are reported by
`GET /api/staff/storage` (`totals.attachment_metadata_without_content`) and
refused by the download route with `410 Gone` rather than served from a stale
copy. If that legacy data must be preserved, extract the objects with the old
deployment's tooling while it is still running, then import them (one row at a
time, encrypted) before decommissioning the object store. `pg_dump` and
`pg_restore_drill.sh` continue to cover everything else, now including all new
binary content.

Keep TLS in front of the `web` container (load balancer, Traefik, or certbot +
Nginx on the host) — the container itself serves plain HTTP on the published
port, and the API sets `Secure` cookies in production.

## What a deploy does

1. The runner packages `docker-compose.prod.yml`, `infra/`, `backend/alembic/`, `Makefile` and this document and uploads it to `DEPLOY_PATH` (code only — no secrets).
2. On the host: `docker compose --env-file .env.production -f docker-compose.prod.yml pull` pulls the images tagged with the **commit SHA**.
3. `docker compose ... run --rm migrate` applies Alembic revisions. Migrations are **forward-only**; a failing migration stops the rollout before any container is replaced.
4. `docker compose ... up -d --remove-orphans` replaces `api`, `worker` and `web`.
5. The job polls `http://127.0.0.1:$HTTP_PORT/api/health/ready` for up to 150 seconds. Readiness checks PostgreSQL through PgBouncer *and* Redis; a failure fails the job and prints `docker compose ps` for triage.
6. Old layers are pruned on the host (`docker image prune --filter until=168h`).

## First administrator (empty database)

Migrations and `up` never create staff accounts or demo data, so a fresh
production database has nobody who can sign in. Create exactly one
administrator with the opt-in `tools` service (it is **not** started by
`up -d`):

```bash
cd /srv/insurance-platform
docker compose --env-file .env.production -f docker-compose.prod.yml \
  run --rm bootstrap_admin
```

The command prompts for a name, e-mail and a 12+ character password (letters
and digits), stores an Argon2id hash, creates the `administrator` role with the
full permission catalog and grants global all-companies/all-ownership scope.
It creates **no** insurers, customers, complaints, teams or demo rows, and it
refuses to run when any staff account already exists (exit code `1`), so it
cannot silently add a second privileged identity. For automation, set
`BOOTSTRAP_ADMIN_NAME`, `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`
in the invocation environment and add `--non-interactive` plus `-T`; never put a
real password in the compose file or a workflow log. Afterwards sign in at
`/staff/login`; sessions and the login timestamp are recorded on first use.

`seed` is a development-only alternative and must never run against this
database (it adds demo accounts and closes the bootstrap path).

## Rollback

Images are immutable and tagged by commit SHA, so a rollback is a tag change —
no rebuild:

```bash
cd /srv/insurance-platform
PREVIOUS=<sha of the last known-good release>   # `gh run list` or the release notes
IMAGE_TAG="$PREVIOUS" docker compose --env-file .env.production -f docker-compose.prod.yml up -d
```

Rollback restores code, not schema. Because the SQL revisions are forward-only,
roll back to a release whose schema expectations still hold, or restore the
database from backup (see below). Never edit a released revision in place.

## Backups and drills

* `scripts/backup/pg_backup.sh` makes a custom-format `pg_dump` with a SHA-256
  file, optional gpg encryption (`BACKUP_GPG_RECIPIENT`), optional off-site
  upload (`BACKUP_S3_URI`, an operator-owned *backup* destination — not
  application storage) and local retention. Run it as the migrator or
  superuser role on a schedule (cron, or a Kubernetes CronJob). `make backup`
  wraps it for the compose stack.
* `scripts/backup/pg_restore_drill.sh <dump>` restores into a scratch database
  and verifies the checksum, the Alembic revision, that RLS is still enabled,
  and that `app_runtime` sees **zero** rows without session context. CI runs
  this drill on every push.
* Prefer managed PostgreSQL with PITR (RPO ≤ 5 min). Logical dumps are the
  portable second line. RPO, RTO and the region-loss procedure are in
  [RUNBOOK.md](RUNBOOK.md#backups-and-disaster-recovery).
* Attachments, export artifacts and call recordings are rows in the database,
  so the PostgreSQL backup (PITR + logical dump) is the complete copy — size the
  dump window for the binary columns. Export artifacts expire 15 minutes after
  completion (`clean_expired_exports` cron). Decompress/decrypt with
  `APP_ENCRYPTION_KEY` only under an approved procedure.

## Kubernetes (Helm)

`deploy/helm/insurance-platform` is the scale-out target. It contains:

* `api`, `worker` and `web` Deployments with HPA and PDB, a `preStop` drain,
  probes, a read-only root filesystem and non-root users;
* PgBouncer (transaction mode);
* a migrate `pre-install`/`pre-upgrade` hook with `lock_timeout=5s`;
* a monthly partitions CronJob;
* Ingress and NetworkPolicies;
* optional `ServiceMonitor`, `PrometheusRule` and Grafana dashboard
  ConfigMaps.

```bash
kubectl create namespace insurance
# Secrets come from your secret manager (External Secrets / Sealed Secrets / Vault CSI).
# Keys: APP_DB_PASSWORD, APP_WORKER_DB_PASSWORD (URL-safe), DATABASE_ADMIN_URL,
# APP_ENCRYPTION_KEY, APP_BLIND_INDEX_KEY, JWT_SIGNING_KEY, JWT_PRIVATE_KEY_B64,
# JWT_PUBLIC_KEY_B64, METRICS_TOKEN, TURNSTILE_SITE_KEY, TURNSTILE_SECRET_KEY,
# SMTP_PASSWORD, ...
# Generate a fresh Ed25519 key pair with: python scripts/generate_jwt_keys.py
helm upgrade --install ip deploy/helm/insurance-platform -n insurance \
  -f deploy/helm/insurance-platform/values-production.yaml \
  --set image.tag=<release tag or commit SHA>
```

Every push to CI runs `helm lint --strict`, `helm template` and `kubeconform`
for both value files. The chart's `files/alerts.yaml` and
`files/grafana-dashboard.json` must stay identical to `infra/monitoring/`; CI
compares them.

## Monitoring

* **Prometheus:** scrape `api:8000/metrics` with `Bearer $METRICS_TOKEN`, and
  the worker on port 9100. See `infra/monitoring/prometheus.yml`, or the
  chart's ServiceMonitors.
* **Alert rules:** `infra/monitoring/alerts.yaml` covers availability (5xx
  ratio, p95 latency, queue depth and backpressure) and security (suspicious
  access, denial spikes, lockouts, auth and CAPTCHA failures, export bursts,
  rate-limit storms). Each rule links to [RUNBOOK.md](RUNBOOK.md).
* **Grafana:** import `infra/monitoring/grafana/insurance-platform.json`.
* **Sentry:** set `SENTRY_DSN`.
* **Logs:** JSON lines on stdout with `request_id` and `user_id`.

## Honest limits

* The pipeline is production-shaped. Phase 8 adds key rotation, backups with restore drills, monitoring, load tests and PDPL tooling, but TLS certificates, an external penetration test, the PDPL legal review and a capacity test on production-sized hardware are still required before go-live.
* `postgres-rls` and the container smoke tests run on GitHub runners; a green pipeline here does not replace a staged rehearsal on the real host.
* Deployment uses SSH from the runner. When nobody has supplied `DEPLOY_SSH_KNOWN_HOSTS`, the first connection accepts a new host key; pin the key before treating the path as trusted.


## Staff invitations and employee profiles

1. Apply Alembic through `20261018_29` before deploying the new UI. Existing staff
   (and bootstrap/seed accounts) belong to **الإدارة العامة** until an administrator
   assigns their actual department under **Users → employee photo → department**.
   Legacy accounts are explicitly shown as incomplete; no invented identity/photo
   is backfilled. New invitations cannot activate without all required details/photo.
2. Configure and enable a **workplace/global** SMTP, Gmail OAuth, Outlook OAuth,
   SendGrid or SES connection in **Settings → Integrations** and run its connection
   test. Account mail uses the most recently updated connected workplace email
   provider (then the most recently configured enabled one). A failed selected
   provider fails closed; it never falls back to a test address. Company-only
   connections and `sandbox_recipient` are not used for account invitations.
3. If no workplace email connection is enabled, account mail uses the explicit
   `SMTP_*` environment settings. Production refuses local/Mailpit/Mailtrap/sandbox
   SMTP hosts, test senders and unencrypted SMTP for account mail. For port 587 use
   `SMTP_START_TLS=true`, `SMTP_USE_TLS=false`; for implicit TLS on 465 use
   `SMTP_START_TLS=false`, `SMTP_USE_TLS=true`. `DEV_SMS_MAILBOX` must be false in
   production. Keep provider credentials outside Git/chat; use the existing secret
   store or the encrypted integrations form.
4. Set `PUBLIC_APP_URL` to the real **HTTPS origin** serving the SPA, not localhost,
   the API-only host or a test URL. A one-time `/invite#token=...` link expires in
   48 hours; the fragment avoids access-log/referrer leakage. Existing `?token=`
   links remain supported. Expired/pending invites can be re-sent in Users; the
   previous token is invalidated atomically. A provider rejection rolls back the
   new user/invitation (or reset/resend) rather than reporting success.
5. Use a verified sender/domain (SPF, DKIM and DMARC). SES accounts must be out of
   the SES sandbox to reach arbitrary addresses. Gmail/Outlook OAuth requires the
   actual mail-send scopes. A successful API response means **provider acceptance**,
   not proof of inbox delivery: check provider delivery/bounce logs and Spam/Junk.
   Run a deployment acceptance test by inviting an address you control on Gmail
   and on your company domain; open the actual email, complete the form, sign in,
   and click the photo from `/staff/admin` and `/staff/users`.

The invitation form collects full name, Egyptian national ID, mobile, address,
city/governorate, optional job title/emergency phone and a required JPEG/PNG/WebP
photo (512 KiB maximum). Birth date is derived from the validated national ID.
Email and department come from the invitation, not the public request. Images
are decoded, bounded, resized to 512×512 and re-encoded as metadata-free JPEG;
SVG/animation/corrupt images are refused. Both identity JSON and photo are
row-bound AES-GCM encrypted inside PostgreSQL and included in key rotation.
Directory lists expose no national ID/address/phone. Profile/photo reads require
`users.view` **and global scope**; department writes require `users.edit` and CSRF.
Departments never grant role/team/complaint access. Do not cache these private APIs.

Automated evidence: `backend/tests/test_staff_onboarding*.py` covers provider
routing, SMTP recipient envelopes, transaction rollback, invitation replay/expiry,
validation, encrypted photos, departments and authorization. The integration file
uses a disposable real PostgreSQL database and a local SMTP server, **not Gmail**.
Browser coverage is in `frontend/e2e/onboarding.spec.ts`; a separate live inbox
acceptance test is still required after deploying real provider credentials.
