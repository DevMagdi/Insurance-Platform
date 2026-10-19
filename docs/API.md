# API guide

The **authoritative contract** is [`openapi.yaml`](openapi.yaml), generated
from the running FastAPI app. CI fails if the built image's `app.openapi()`
drifts from it. Interactive docs are served at `/docs` and `/redoc` outside
production. Production hides them, along with `/openapi.json`. Regenerate the
file with:

```bash
cd backend && ../.venv/bin/python ../scripts/export_openapi.py   # docs/openapi.yaml
cd frontend && npm run api:types                                  # typed client
```

## Conventions

* **Base path:** `/api/*`, same-origin behind Nginx. (The `/api/v1/*` paths in
  `openapi-roadmap.yaml` are a draft and not live.) The partner API is
  `/api/public/v1/*` with `X-API-Key`.
* **Auth:** HttpOnly `access_token` (15 min) and `refresh_token` cookies
  (rotated via `POST /api/auth/refresh`), plus a readable `csrf_token` scoped
  to `/` so the SPA can read it. Every authenticated
  `POST`/`PUT`/`PATCH`/`DELETE` needs `X-CSRF-Token: <csrf_token cookie>`.
  Existing `/api`-scoped CSRF cookies are migrated on the next page load.
* **CAPTCHA:** public forms (`/api/auth/customer/otp/start`,
  `/api/guest/track`, staff login, invite accept) need a Turnstile token in
  `X-Captcha-Token` when CAPTCHA is configured. `GET /api/public/config`
  returns `{captcha: {provider, site_key} | null, privacy_policy_version}`.
* **Idempotency:** `POST /api/customer/complaints` requires
  `Idempotency-Key` (8–128 chars). A replay with the same body returns the
  original ticket. A different body with the same key returns `409`.
* **Pagination:** `page` / `page_size` (bounded), with `total` in the response.
  Analytics endpoints require a bounded `date_from` / `date_to` window.
* **Errors:** FastAPI shape `{"detail": ...}`.

  | Status | Meaning |
  |---|---|
  | `401` | not authenticated |
  | `403` | forbidden, CSRF or CAPTCHA failure |
  | `404` | not found, **or out of scope** (indistinguishable by design) |
  | `409` | conflict |
  | `413` | body too large |
  | `422` | validation |
  | `429` | rate limited or account locked out (with `Retry-After`; rate limits also send `X-RateLimit-Limit` and `X-RateLimit-Remaining`) |
  | `503` | queue backpressure or draining (with `Retry-After`) |
* **Tracing:** send or receive `X-Request-ID`. It appears in every log line
  and in Sentry.
* **Caching:** `/api/companies` is `public, max-age=300, stale-while-revalidate=60`.
  `/api/public/config` is `no-store` (the CAPTCHA configuration must never be cached).
  Everything else under `/api` is `no-store`.

## Phase 8 endpoints

| Method and path | Who | Purpose |
|---|---|---|
| `GET /api/public/config` | anyone | CAPTCHA site key and privacy-policy version |
| `GET /api/customer/privacy/consents` | customer | current consent per purpose |
| `POST /api/customer/privacy/consents` | customer (CSRF) | record a grant or withdrawal `{purpose, granted}` |
| `GET /api/customer/privacy/export` | customer | right-of-access JSON export |
| `GET`/`POST /api/customer/privacy/requests` | customer (CSRF) | list or create `{kind: access, erasure, rectification or objection, details}` |
| `GET /api/staff/privacy/requests` | `settings.manage` with global scope | DPO queue (`?status=`) |
| `POST /api/staff/privacy/requests/{id}` | `settings.manage` with global scope | `{status: in_progress, completed or rejected, resolution}`; completing an erasure anonymises the customer |
| `GET /metrics` | Prometheus with bearer `METRICS_TOKEN` (required in production) | metrics (not in the OpenAPI contract) |
| `GET /api/health/live`, `GET /api/health/ready` | probes | liveness; readiness (DB through PgBouncer, plus Redis; `503` while draining) |

## Phase 9 endpoint

| Method and path | Who | Purpose |
|---|---|---|
| `GET /api/staff/storage?prefix=&limit=100&continuation_token=` | `storage.manage` with global scope | Bounded, audit-logged metadata inventory of the platform's own PostgreSQL rows (attachments, exports, call recordings); does not return file bodies or permit mutations |

See [STORAGE_ADMIN.md](STORAGE_ADMIN.md) for the built-in `/staff/storage` UI, access model and intentional privacy limits.

## Live console endpoints

The staff console updates without a browser refresh: the complaints queue
streams over the staff WebSocket, an open ticket streams over SSE, and the
directory pages poll these cheap revision signatures (they apply the same
permission/scope gate as the directory they version, and return an empty
`revision` when the caller may not read it).

| Method and path | Who | Purpose |
|---|---|---|
| `GET /api/staff/users/revision` | `users.view` | Change signature for the staff directory; the Users page refetches only when it moves |
| `GET /api/staff/customers/revision` | `users.view` with global scope | Change signature for the customer directory; the Customers page refetches only when it moves |
| `GET /api/me/notifications` | staff | Live notice list (bell); every item carries `complaint_id` so the console can open that ticket's chat |
| `GET /api/staff/notifications` | `complaints.view` | Same scoped notice list for the staff API surface |

When a customer creates a complaint or sends a message, the server fans a
scoped notification out to every active staff member who holds
`complaints.view` and whose AccessScope covers the ticket (deny overrides
win). The recipient set is derived inside a `SECURITY DEFINER` function, so a
customer can never name a recipient and out-of-scope staff never learn the
ticket exists.

Rate limits per endpoint class are listed in
[SECURITY.md](SECURITY.md#rate-limiting-and-abuse-detection). The access model
behind every staff route is in [ACCESS_CONTROL.md](ACCESS_CONTROL.md).
