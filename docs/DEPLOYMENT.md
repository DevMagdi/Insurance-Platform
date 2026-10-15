# Deployment runbook (CI/CD)

Two workflows own the lifecycle:

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | every push and pull request | Ruff, ruff-format, strict mypy, offline pytest with an 80% coverage gate, frontend lint/typecheck/Vitest/build, **container build + smoke test of both images** and a `docker compose config` render of the production file, then the real PostgreSQL 16 + PgBouncer RLS/migration/seed suite |
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
HTTP_PORT=8080
APP_WORKER_DB_PASSWORD=...
```

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

* `docker compose ... exec postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > backup.dump` on a schedule, stored off-host with encryption.
* Snapshot the `minio_data` volume for attachments; exports live under the `exports/` prefix and expire through the worker cron.
* Restore drills are part of the release checklist: restore into a scratch host, run `alembic upgrade head` and the integration suite, and confirm RLS still blocks cross-company reads.

## Honest limits

* The pipeline is production-shaped, not a claim of production readiness: TLS, key rotation, backups, monitoring, load testing and the PDPL review are still required.
* `postgres-rls` and the container smoke tests run on GitHub runners; a green pipeline here does not replace a staged rehearsal on the real host.
* Deployment uses SSH from the runner. When nobody has supplied `DEPLOY_SSH_KNOWN_HOSTS`, the first connection accepts a new host key; pin the key before treating the path as trusted.
