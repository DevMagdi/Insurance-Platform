# Customer portal (Phase 6)

Mobile-first, Arabic-first (RTL) with full English (LTR), dark/light, premium
navy + gold/emerald glassmorphism. Implemented in `frontend/src` with React +
Vite + Tailwind, TanStack Query, Zod validation and Framer Motion. All browser
traffic is same-origin `/api/...`; the dev server/Nginx proxies it to FastAPI.

## Journeys

| Route | Journey |
|---|---|
| `/` | Landing with hero CTAs plus an embedded guest-tracking form |
| `/complaints/new` | 5-step wizard: identity → policy → complaint → attachments → review |
| `/tickets` | My Tickets: counters, search/filter, pagination |
| `/tickets/:id` | Animated timeline, assigned agent, replies, attachments, reopen, CSAT, live SSE |
| `/track` | Guest tracking with ticket number + national ID (both required, server-side) |
| `/login` | Mobile OTP sign-in; first-time registration (name + national ID + WhatsApp toggle) |
| `/profile` | Profile edit, notification preferences (email/WhatsApp/SMS), notifications + read receipts |

## Complaint wizard

- **Live validation** mirrors the backend: Egyptian national ID (14 digits,
  birth-century/date/governorate checks — port of
  `backend/app/security/national_id.py`), `+201…` mobile normalization,
  policy-number/subject/description lengths. The ID is only ever displayed
  masked (`•••• •••• ••23 45`).
- **Insurance type** options (Health / General / Both) are filtered by the
  selected company's `supported_types` from `GET /api/companies`.
- **Attachments** are queued locally (drag & drop, image previews, 5 files /
  10 MB each / 25 MB total, PDF/PNG/JPG — the backend plan limits). Uploads
  happen after the complaint exists: checksum (SHA-256) → plan → one-use
  signed `PUT` with XHR progress, per-file remove/retry. Files stay
  `pending` until a virus scanner marks them `clean`; downloads are blocked
  before that, exactly as the backend enforces.
- **Privacy-preserving resume metadata**: only the wizard step and stable
  `Idempotency-Key` are persisted in `localStorage` (350 ms debounce). All
  customer-entered fields, including the national ID, contact details, free
  text and attachment names, stay in memory and are never autosaved. A reload
  therefore requires the customer to re-enter their details and re-attach files.
- **Guests** verify the mobile with an OTP inline at the review step; the
  verify call registers the customer on first use, then the complaint is
  created in the same flow. The success screen shows the ticket number.

## Realtime

`GET /api/customer/complaints/{id}/stream` is a same-origin SSE endpoint
(cookie auth). Every poll opens a fresh scoped transaction and re-runs the
session/token-version/ownership checks (the staff-WebSocket discipline), so
revocation stops the stream. Events carry only status + a timeline signature
— never message bodies; the client refetches through the scoped REST API.
The detail page shows a Live/Offline indicator and keeps a 30 s polling
fallback when SSE is disconnected.

## Phase 6 backend additions

- Migration `20261016_18`: `customers.notification_preferences` plus
  SECURITY DEFINER helpers `customer_profile()`,
  `update_customer_profile(...)`, `assigned_agent_display(...)` and
  `mark_customer_notification_read(...)` — `app_runtime` gains no blanket
  UPDATE on customers/notifications.
- `GET/PATCH /api/customer/profile`, `PATCH /api/customer/notifications/{id}/read`,
  and complaint detail now includes `assigned_agent_name`, `csat_score`,
  `can_reopen` / `can_csat` (mirroring the SQL reopen/CSAT windows: 14 days /
  max 3 reopens; 30 days for CSAT).

## Tests

- Vitest component/unit: `cd frontend && npm run test` — validation ports,
  draft autosave, file limits, timeline mapping, `StatusTimeline`,
  `FileUploader`, the full wizard walk, language switching, guest tracking.
- Playwright e2e: `npm run test:e2e` — Chromium (mobile + desktop) with the
  API mocked at the network layer (CI installs the browser and runs them;
  the sandbox used for this phase cannot download Chromium from its CDN).
- Backend: `backend/tests/test_phase6_portal.py` covers profile schemas,
  definer-only repository access, reopen/CSAT window parity and the SSE
  generator (snapshot → update → heartbeat, staff-claim rejection).
