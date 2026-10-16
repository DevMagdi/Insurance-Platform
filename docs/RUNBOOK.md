# Operations runbook

On-call procedures for the Insurance Complaint Platform. The anchors in this
file are referenced by `infra/monitoring/alerts.yaml`, so keep the headings
stable.

* **Dashboards:** Grafana → *Insurance Platform* (uid `insurance-platform`,
  from `infra/monitoring/grafana/insurance-platform.json`).
* **Logs:** JSON on stdout. Filter by `request_id` (also returned to clients
  as `X-Request-ID`), `user_id`, `route`, `status` and `event`.
* **Errors:** Sentry (PII scrubbed).
* **Deploy and rollback:** [DEPLOYMENT.md](DEPLOYMENT.md).
* **Scaling:** [SCALABILITY.md](SCALABILITY.md).
* **Security controls:** [SECURITY.md](SECURITY.md).

General first steps for any page:

1. Acknowledge the page.
2. Open the dashboard at the alert time.
3. Check `/api/health/ready`. It returns `503` while the pod is draining, or
   when PostgreSQL (through PgBouncer) or Redis is unreachable.
4. Check whether a deploy happened in the last hour (`gh run list -w CD`).
   Roll back by image tag if it lines up.

## API errors

Alert: `ApiHighErrorRate` (5xx ratio > 2% for 5 min).

1. In Grafana, check *HTTP 5xx by route*. One route usually points to a code
   or dependency issue. Every route usually points to infrastructure.
2. Logs: `level=error`, grouped by `route`. Sentry shows the exception and the
   release.
3. Dependency checks:
   * PgBouncer `SHOW POOLS;`: `cl_waiting > 0` for a sustained period means the
     pool is too small or a query is slow;
   * PostgreSQL `pg_stat_activity` for locks;
   * Redis `INFO` (memory, evictions).
4. Recent migration? Migrations run with `lock_timeout=5s`. A failed hook
   leaves the old pods serving, so check the migrate Job logs.
5. Mitigate:
   * roll back the image (`IMAGE_TAG=<previous>`, or `helm rollback`);
   * scale the API (`kubectl scale deploy/<release>-api --replicas=N`; the
     HPA takes over again afterwards).

## Latency

Alerts: `ApiLatencyP95High` (p95 > 1 s), `DashboardLatencyP95High`
(analytics p95 > 2 s).

1. Is it CPU saturation (HPA at max, throttling) or a slow query? Compare pod
   CPU with `http_request_duration_seconds` by route.
2. Slow query: `pg_stat_statements` ordered by `mean_exec_time`. Then
   `EXPLAIN (ANALYZE, BUFFERS)` as `app_runtime` after
   `SELECT set_config('app.user_id', '<id>', true), set_config('app.scope', 'staff', true)`
   inside a transaction.
   * Complaint queries must show `InitPlan` nodes and **no**
     `can_read_complaint` filter (migration 022).
   * If a per-row SECURITY DEFINER filter shows up again, a policy regressed.
3. Dashboards: check `cache_requests_total{cache="analytics",result}`. A low
   hit ratio after a deploy is expected for 5 minutes. If it persists, check
   Redis.
4. Is the read replica lagging (`pg_stat_replication.replay_lag`)? Analytics
   can be moved back to the primary by unsetting `DATABASE_READ_URL`.
5. Mitigate: scale the API or PgBouncer, add the missing index with
   `CREATE INDEX CONCURRENTLY` in a reviewed migration, or temporarily raise
   cache TTLs.

## Queue backlog

Alerts: `QueueBacklogGrowing` (`queue_depth` rising for 15 min),
`QueueBackpressureActive` (rejections are being returned).

1. In the dashboard, see which queue is growing (`queue_depth{queue}`):
   notifications, integrations, exports.
2. Worker health: pod restarts and logs (`event=worker.*`). Is the provider
   (SMTP, WhatsApp, LLM, voice) failing? Look for retries and dead letters.
3. Backpressure is protective. New AI, export and voice jobs receive `503`
   with `Retry-After` while depth > `QUEUE_BACKPRESSURE_THRESHOLD`. Customer
   complaint submission is **never** blocked by backpressure.
4. Mitigate:
   * scale workers (the HPA covers CPU; a backlog caused by provider latency
     needs manual scaling);
   * fix or disable the failing connection in Admin → Integrations;
   * replay dead letters after the fix (audited).

## Suspicious access

Alerts: `SuspiciousAccessPattern`, `AccessDeniedSpike`, `SensitiveExportBurst`.

These signal possible IDOR or enumeration attempts, a compromised staff
account, or bulk data exfiltration.

1. Identify the actor from the logs: `event=security.suspicious_access`, plus
   `event=http.access` with status 401/403/404. The fields are `user_id`,
   `actor_kind`, the hashed identity, and `route`.
2. For staff actors, review the audit trail (Admin → Audit or
   `/api/staff/audit/events`): exports, ID reveals, access changes in the last
   24 h.
3. Contain:
   * revoke all of the user's sessions and disable the account (Admin →
     Users). The token_version bump makes every outstanding token invalid on
     the next request; WebSockets close on the next poll;
   * for API keys, revoke the key (Admin → API keys);
   * the abuse detector already blocks the identity for 15 minutes. Extend this
     at the edge (WAF/nginx deny) if the source continues.
4. If personal data may have been disclosed, start the
   [PDPL breach procedure](PRIVACY.md#breach-notification) and preserve the
   logs and the audit export.

## Brute force

Alerts: `BruteForceLockouts`, `AuthFailureSpike`, `CaptchaFailureSpike`.

1. Distinguish credential stuffing (many identities, few IPs or a botnet) from
   targeted attacks (one identity) using `auth_failures_total{kind}` and
   `account_lockouts_total{kind}`.
2. Lockouts are progressive (5 failures, then 5 min, doubling up to 24 h).
   Look for `event=security.lockout` in the logs.
   Responses are uniform, so attackers cannot tell whether an account exists.
3. A CAPTCHA failure spike usually means a bot. Confirm that the Turnstile
   widget still loads (CSP allows `challenges.cloudflare.com`) and that the
   secret key is valid. A misconfigured key looks the same as an attack.
4. Mitigate:
   * tighten nginx `limit_req` for `/api/auth/` or enable the WAF bot rules;
   * force a password reset for targeted staff and require 2FA (Admin →
     Settings → enforce staff 2FA).
5. Unlock a legitimate user only after verifying their identity out of band.
   Delete the lockout key in Redis, or wait for expiry.

## Rate limits

Alert: `RateLimitStorm` (`rate_limited_total` well above baseline).

1. Check which category (`rate_limited_total{category}`) and which identity.
   Logs carry `event=http.access` with `status=429`.
2. A legitimate integration (public API key) hitting `public_api`: agree on a
   quota and issue a dedicated key. Never raise the global limits for a single
   tenant.
3. Many users behind one carrier-grade NAT IP hitting `guest_track` or `auth`:
   confirm that `TRUSTED_PROXY_CIDRS` includes the load balancer or CDN ranges,
   so the real client IP is used.
4. Limits are code-reviewed constants (`app/middleware/rate_limit.py`).
   `RATE_LIMIT_MULTIPLIER` exists only for non-production load tests;
   production refuses any value other than 1.

## Encryption key rotation

Rotate `APP_ENCRYPTION_KEY` yearly, and immediately if exposure is suspected.
It encrypts national IDs, TOTP secrets, integration credentials, queued
payloads, AI insights, transcripts and recordings.

1. Generate a key:
   `python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
2. The first time you rotate, set `APP_IDEMPOTENCY_KEY` to the **current**
   key, so integration de-duplication digests stay stable.
3. Deploy the API **and** the worker with:
   * `APP_ENCRYPTION_KEY=<new>`;
   * `APP_ENCRYPTION_KEY_ID=<previous id + 1>`;
   * `APP_ENCRYPTION_PREVIOUS_KEYS=<previous id>:<old key>` (comma-separate
     older entries).

   New writes use the new key; old ciphertext stays readable.
4. Re-encrypt existing rows using the migrator role (`DATABASE_ADMIN_URL`):
   `python -m app.scripts.rotate_keys`. It runs online in committed batches
   and is restartable.
5. Verify with `python -m app.scripts.rotate_keys --check`. It must report
   0 stale values.
6. Remove the old key from `APP_ENCRYPTION_PREVIOUS_KEYS` and redeploy.
   `rotate_keys` walks every encrypted column — including the private binaries
   held in PostgreSQL (`attachments.content`, `export_jobs.content` and
   `call_logs.recording_content`) — so once `--check` reports 0 stale values no
   old key is needed anywhere.

`JWT_SIGNING_KEY` rotation invalidates every session (users sign in again), so
do it in a quiet window. `APP_BLIND_INDEX_KEY` cannot be rotated online (see
[SECURITY.md](SECURITY.md#residual-risks-and-required-external-work)).

## Backups and disaster recovery

| Target | Value |
|---|---|
| RPO (data loss) | ≤ 5 min with WAL archiving / PITR (managed PostgreSQL or pgBackRest); ≤ 24 h with nightly logical dumps only |
| RTO (service restored) | ≤ 1 h for a single-region failure: restore or promote, then redeploy by image tag |
| Backup retention | 35 days PITR; nightly logical dumps for 14 days locally and 90 days off-site (encrypted, separate account or region) |
| Drill cadence | Restore drill on every CI run (`postgres-rls` job) and a full staged restore monthly |

* **Logical backup:** `scripts/backup/pg_backup.sh`. It produces a
  custom-format dump (now including every attachment, export artifact and
  recording, since they are rows) with a SHA-256 checksum, optional gpg
  encryption (`BACKUP_GPG_RECIPIENT`) and an optional off-site copy
  (`BACKUP_S3_URI`) whose destination is an operator-owned backup bucket — it
  is not application storage and the API never depends on it. It must run as
  the migrator or superuser role: the runtime role is RLS-filtered and would
  produce a partial dump. Use `make backup` for the local stack.
* **Restore drill:** `scripts/backup/pg_restore_drill.sh <dump>`. It restores
  into a throw-away database, then verifies:
  * the checksum;
  * the Alembic revision (`EXPECTED_REVISION`);
  * that RLS is still enabled and `app_runtime` cannot bypass it (zero rows
    without session context);
  * the minimum row counts.

  CI runs it after every integration run.
* **Private binaries:** attachments, export artifacts and call recordings are
  rows in the primary database, so they follow PostgreSQL's own
  replication/PITR story — there is no bucket to version or replicate and no
  second store to keep consistent. `attachments.content` is immutable and the
  SHA-256 checksum of the plaintext stays beside it in the row, so a restored
  copy can prove exactly what it holds.
* **Redis:** holds no system-of-record data (limits, OTP, caches, ARQ
  scheduling). It can be lost: users re-request OTPs and caches rebuild. The
  durable queues are in PostgreSQL.
* **Secrets:** escrow `APP_ENCRYPTION_KEY` (all ids still in use),
  `APP_BLIND_INDEX_KEY` and `JWT_SIGNING_KEY` in the KMS or secret manager,
  with break-glass access. **A database backup without its keys is
  unreadable.**

### Region or primary loss

1. Promote the replica, or restore PITR into a new primary.
2. Point the PgBouncer `DB_HOST` (Helm `database.host`) at it, then roll the
   PgBouncer pods.
3. Run `alembic current` with the migrator. It must equal the image's expected
   head.
4. Check `/api/health/ready`, run the smoke checks (log in, submit, track) and
   watch the error and latency alerts.
5. Rebuild a new replica. Restore `DATABASE_READ_URL`.

## Routine maintenance

* **Partitions:** the CronJob or `make partitions` runs monthly. It creates
  the upcoming monthly partitions. Alert if the DEFAULT partition starts to
  receive rows.
* **Retention:** the worker cron runs `apply_retention` daily at 03:17 UTC
  (see [PRIVACY.md](PRIVACY.md#retention-schedule)).
* **Dependency and image scans:** CI on every push. Rebuild images at least
  monthly to pick up base-image patches.
* **Privacy requests:** `GET /api/staff/privacy/requests` (needs `settings.manage` and global scope). They are due within 30 days; there is no staff UI screen yet.
