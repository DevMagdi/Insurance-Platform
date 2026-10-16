# Security hardening (Phase 8)

Scope: OWASP Top 10 (2021) review of the backend, frontend, edge and delivery
pipeline, plus the automated access-control penetration suite. Access control
is described in [ACCESS_CONTROL.md](ACCESS_CONTROL.md), privacy in
[PRIVACY.md](PRIVACY.md), and incident procedures in [RUNBOOK.md](RUNBOOK.md).

## OWASP Top 10 audit

| # | Risk | Controls (where) | Phase 8 findings → fixes |
|---|---|---|---|
| A01 | Broken access control | `require_permission` on every staff route, plus `AccessScope` predicates, plus PostgreSQL RLS on the same transaction. Customers are bound to `customer_id`. Signed, single-use attachment tokens. WebSocket/SSE re-check access on every poll. | **Fixed:** session revoke did not check the owner (`DELETE /api/me/sessions/{id}`). **Fixed:** attachment `authorize` checked the scan state before scope, which leaked existence; scope is now checked first. Automated pen-test suite added (below). |
| A02 | Cryptographic failures | AES-256-GCM with a **versioned keyring** (`KR` header + key id) and online rotation (`app.scripts.rotate_keys`). Independent HMAC blind index key. Argon2id passwords. API keys stored as SHA-256. TLS required in production (HSTS, `Secure` cookies). | **Added:** key rotation with `APP_ENCRYPTION_PREVIOUS_KEYS`, plus a stable `APP_IDEMPOTENCY_KEY` so de-dup digests survive rotation. **Fixed:** the production compose worker was missing `APP_ENCRYPTION_KEY`. Production refuses to reuse a key across purposes. |
| A03 | Injection | SQLAlchemy bound parameters everywhere; `test_no_dynamic_sql_outside_reviewed_allowlist` fails the build on dynamic (f-string) SQL. Pydantic strict schemas. React escaping. No `dangerouslySetInnerHTML`. CSV formula neutralisation. | Reviewed; no SQLi/XSS found. **Added:** the dynamic-SQL allowlist test. |
| A04 | Insecure design | Guest tracking needs ticket **and** national ID. Same response whether or not an account exists. Idempotent complaint creation. Queue backpressure. Human approval for AI replies. | **Added:** Turnstile CAPTCHA on OTP start, guest tracking, staff login and invite acceptance (`X-Captcha-Token`). Progressive lockout. Abuse detector. |
| A05 | Security misconfiguration | API CSP `default-src 'none'`. SPA CSP self plus Turnstile only. HSTS (`HSTS_SECONDS`), `nosniff`, `DENY` framing, COOP/CORP, Permissions-Policy, `Server` header removed. `/docs` disabled in production. Strict `CORS_ALLOWED_ORIGINS` allowlist (https only in production). Non-root, read-only containers. | **Added:** security header middleware and nginx snippets. **Added:** origin guard (CORS plus a cross-site write block). **Added:** production settings validation that refuses to boot on weak config. **Added:** self-hosted fonts (no third-party CSS). |
| A06 | Vulnerable components | CI: `pip-audit`, `npm audit --audit-level=high`, Trivy fs/secret/config and image scans (fail on fixable CRITICAL, report HIGH). Pinned, checksum-verified scanner binaries. | **Fixed:** bumped `cryptography`, `pytest`, `pip` and `setuptools` after pip-audit findings. **Fixed:** the bundled store runs SeaweedFS with an S3-API bootstrap script. **Residual:** a self-hosted object store is still one more stateful service to patch, so use managed S3 in production. |
| A07 | Identification & auth failures | 15-minute access JWT, rotating refresh with replay-family revocation, token_version checks, TOTP, enforced 2FA. Lockout policies: staff password 5 per 15 min, TOTP 5 per 15 min, OTP verify 8 per hour, per-IP staff login 30 per 15 min. Lock durations double up to 24 h. | **Added:** `LoginLockout` with uniform responses (no e-mail enumeration) and `account_lockouts_total` / `auth_failures_total` metrics. |
| A08 | Software & data integrity | Images built in CI, SHA-tagged, smoke-tested and contract-checked before deploy. Migrations forward-only and linted. Webhook HMAC with timestamps. Idempotency keys. | **Added:** migration lint in CI, and chart-file drift check. |
| A09 | Logging & monitoring failures | JSON logs with `request_id`, `user_id`, route, status and latency. No PII or tokens in logs. Prometheus metrics. Sentry with PII scrubbing (`send_default_pii=False` plus `before_send`). Security alerts. | **Added:** observability stack and suspicious-access alerts ([RUNBOOK.md](RUNBOOK.md)). |
| A10 | SSRF | Outbound webhooks/providers resolve DNS once, reject private, loopback, link-local and metadata ranges, pin the resolved IP, require https and disable redirects (`app/security/ssrf.py`). | **Added:** SSRF guard with tests. |

Other fixes from the audit:

* `client_ip` previously trusted the raw peer. It now honours
  `X-Forwarded-For` only from `TRUSTED_PROXY_CIDRS`, which keeps rate limits
  from being bypassed or poisoned.
* Three preview `POST` endpoints were missing CSRF. Now fixed.

### CSRF and CORS

Authenticated writes use the double-submit token: the readable `csrf_token`
cookie must match the `X-CSRF-Token` header (constant-time comparison).
`SameSite=Lax` and HttpOnly apply to the session cookies.

The origin guard:

* rejects cross-site state-changing requests whose `Origin`/`Sec-Fetch-Site`
  is not on the allowlist;
* answers CORS preflights only for allowlisted origins.

The SPA is same-origin, so the default allowlist is empty.

### Uploads

* Signed one-use upload tokens bound to actor, session and token version.
* Per-file 10 MB, 5 files and 25 MB per complaint.
* Body limit middleware: `Content-Length` is checked first, then streamed
  bodies are counted.
* Magic-byte MIME sniffing against an allowlist; exact SHA-256 checksum.
* Private object storage. Downloads only after a `clean` scanner verdict, with
  `no-store`, `nosniff` and `Content-Disposition: attachment`.
* nginx limits upload rate (2 r/s) and body size per route.

### Secrets management

* No secret in Git, images or workflow logs.
* Compose reads `.env.production` (mode 0600). Kubernetes reads
  `secrets.existingSecret` (Sealed Secrets, External Secrets or Vault CSI).
* The API never receives the admin DB URL or object-store root credentials.
* Trivy's secret scanner runs on every push.
* Production startup validation rejects:
  * placeholder or short keys;
  * reused keys;
  * missing Turnstile keys;
  * `RATE_LIMIT_MULTIPLIER` other than 1;
  * a short `METRICS_TOKEN`;
  * non-https CORS origins.

## Rate limiting and abuse detection

Redis sliding windows (sorted sets; atomic Lua; HMAC-hashed identities so raw
IPs, e-mails and mobiles never appear in Redis keys). The first matching
endpoint class applies, **plus** the global ceilings:

| Class | Limits |
|---|---|
| global | 1,200 per 60 s per IP; 600 per 60 s per user |
| `auth` (`/api/auth/{customer,staff}/*` POST) | 60 per 5 min per IP; plus OTP per mobile 3 per 10 min, OTP per IP 20 per hour, staff login 8 per 15 min per e-mail and 25 per 15 min per IP |
| `guest_track` | 30 per 10 min per IP; plus a per ticket+ID pair limit |
| `complaint_submit` | 20 per hour per user; 100 per hour per IP |
| `upload` | 60 per hour per user; 200 per hour per IP |
| `export` (exports, CSV/XLSX audit, report runs) | 30 per 10 min per user |
| `pii_reveal` | 30 per hour per user |
| `ai_call` (AI analyse/send, voice, email, WhatsApp) | 60 per 10 min and 600 per day per user |
| `privacy_export` / `privacy_request` | 5 per hour / 5 per day per user |
| `public_api` | 300 per 60 s per API key; 900 per 60 s per IP |
| `webhook` | 600 per 60 s per IP |

Responses are `429` with `Retry-After` and `X-RateLimit-*` headers, counted in
`rate_limited_total{category}`.

**Abuse detector:** an identity that produces a burst of 401/403/404 responses
(`ABUSE_DENIED_THRESHOLD` in 5 min, doubled for anonymous IPs) is blocked for
15 minutes. This increments `suspicious_access_total{reason}` and logs
`security.suspicious_access`. It catches ID enumeration (the pen-test suite
asserts 16×404 then 429) and scope probing.

**Request size limits:** JSON bodies are capped at `MAX_JSON_BODY_BYTES`
(1 KiB–10 MiB, default 1 MiB). Uploads are capped at the per-file ceiling.
Gunicorn limits request line and header sizes. nginx limits per-route body
size.

## Automated access-control penetration suite

`backend/tests/test_phase8_pentest.py` runs against real PostgreSQL, through
both the direct connection and PgBouncer, in the `postgres-rls` CI job. It also
covers unit-level checks in `test_phase8_security.py`.

| Attack | Expected | Covered |
|---|---|---|
| Customer IDOR on another customer's complaint, timeline, attachments, CSAT/reopen | 404, no data | ✅ |
| Horizontal escalation: agent reads, replies to or changes status of an out-of-scope company/type/owner ticket | 404/403 | ✅ |
| Scope bypass via search `q=`, filters, counters, CSV/XLSX export, child endpoints | out-of-scope rows never appear | ✅ |
| Scope bypass via cache: analytics cache shared between differently scoped staff | separate keys, different results | ✅ |
| Scope bypass via attachment URLs: out-of-scope `authorize` (pen-test); token replay, other session, missing CSRF (`test_phase2_api`, `test_phase2_attachments`) | 403/404 | ✅ |
| WebSocket stream re-authorisation on permission revoke | socket closed | ✅ (Phase 2 tests) |
| Vertical escalation: agent edits own roles, grants permissions above own, removes last admin | 403 | ✅ |
| Stale permissions after revocation: revoke a permission or scope, then reuse the old token | denied on the next request (token_version + versioned snapshot) | ✅ |
| Enumeration: sequential IDs or tickets | 16×404 then 429 (abuse block) | ✅ |
| Brute force: 5 bad passwords | 401 ×5 then 429 lockout, uniform for unknown e-mails | ✅ |
| CSRF missing or mismatched; cross-site Origin on writes; disallowed CORS preflight | 403 | ✅ |
| Oversized bodies (declared and streamed) | 413 | ✅ |
| Privacy endpoints bound to the authenticated customer | other customers' requests invisible | ✅ |
| RLS under load: 100 interleaved requests (20 in flight) with mixed actors, direct and through PgBouncer | zero cross-actor leakage | ✅ |
| InitPlan RLS policy equivalent to the reference predicate for every actor | identical row counts | ✅ |
| Key rotation: old ciphertext readable, re-encrypted under the new key | ✅ | ✅ |

Findings fixed while building the suite: session revoke owner check, attachment
authorize ordering, CSRF on preview endpoints, `client_ip` trust.

## Residual risks and required external work

* An independent penetration test and PDPL legal review before go-live.
* Managed S3 instead of the self-hosted SeaweedFS store.
* A real AV scanner integration (uploads stay pending until one exists).
* `APP_BLIND_INDEX_KEY` cannot be rotated online. Rotating it requires
  recomputing every blind index in a maintenance job. Treat it as long-lived
  and keep it in a KMS/HSM.
* Turnstile depends on Cloudflare. If it is unavailable, protected forms fail
  closed in production.
