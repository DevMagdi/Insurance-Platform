# Staff portal and AI assistance

The staff workspace is part of the same Arabic-first RTL / English LTR React
application as the customer portal. All browser calls remain same-origin
`/api/...`; no browser code talks to a backend host directly.

## Routes

| Route | Purpose | Main permission |
|---|---|---|
| `/staff/login` | Staff email/password login, forced password change, TOTP verify and TOTP enrollment challenges | public challenge flow |
| `/staff` | Scoped complaint queue, counters, filters, saved views, bulk assign/status, staff notifications and WebSocket live refresh | `complaints.view` |
| `/staff/tickets/:id` | Detail, timeline, attachments, public replies/internal notes, status, assignment, escalation, merge, duplicate candidates, canned replies, national-ID reveal and AI review | endpoint-specific permissions |
| `/staff/analytics` | Bounded-window KPIs, daily arrivals, status/company/type breakdowns, top issues and insurer scorecards | `analytics.view` + `complaints.view` |
| `/staff/admin` | First-party, Arabic RTL admin dashboard: live complaint KPIs, intake trend, case/company breakdown, team summary and permission-filtered shortcuts | global scope + `analytics.view` + `complaints.view` |
| `/staff/users`, `/staff/roles`, `/staff/operations` | Staff accounts and access, roles, settings and catalog administration | endpoint-specific admin permissions |
| `/staff/audit` | Audit events, access-change log and metadata-only CSV export | `audit.view` |
| `/staff/storage` | First-party, read-only inventory of the private binaries stored in PostgreSQL (attachments, exports, recordings) | `storage.manage` and global scope |
| `/staff/exports` | Async complaint/analytics export jobs and private downloads | `complaints.export` or `analytics.export` |

Navigation is permission-filtered from the effective access snapshot returned
by `GET /api/me`; backend permission dependencies and PostgreSQL RLS remain the
authority. The queue uses the staff WebSocket snapshot stream and invalidates
the scoped REST queries when updates arrive.

### Everything updates live — no refresh button

| Surface | Live mechanism |
|---|---|
| Complaint queue / inbox | staff WebSocket snapshot per filter; a 20s REST poll only while the socket is down |
| Open ticket | SSE signature stream (`/api/staff/complaints/{id}/stream`); a 15s REST poll only while SSE is down |
| Customer directory | 15s poll of `GET /api/staff/customers/revision`; rows refetch only when the signature moves |
| Users / staff directory | 15s poll of `GET /api/staff/users/revision`; rows refetch only when the signature moves |
| Admin dashboard / analytics | 30s refresh while the tab is visible, plus an immediate refresh on window focus |
| Notification bell | `GET /api/me/notifications` every 10s while visible, immediately on tab focus and on the queue/ticket change signals |

Every live path re-authenticates through the normal session and CSRF flow and
re-runs the same permission and AccessScope checks as a first load, so a
revoked session or a narrowed scope simply stops returning data. A hidden tab
backs off completely; becoming visible refreshes immediately.

When a customer creates a complaint or sends a message, the server writes a
scoped staff notification (see
`app_security.queue_staff_notice`): every active staff member holding
`complaints.view` whose AccessScope covers the ticket receives one, deny
overrides included in the decision. Clicking that notification marks it read
and opens the complaint's chat — the link is the notification's own
`complaint_id`, never a client-side guess.

The storage route is a first-party page in the same product shell, not an
embedded third-party storage console or dashboard. It lists metadata
from the platform's own encrypted database rows, requires `storage.manage` plus
global staff scope, and records successful inventory views. It is deliberately
read-only: object contents and mutations stay behind complaint, scan, retention
and export authorization flows. See [STORAGE_ADMIN.md](STORAGE_ADMIN.md).

## AI insights

AI uses the canonical Phase 4 asynchronous integrations boundary. Migration
`20261015_17` keeps `ai_insights` writes inside the encrypted integration-job
flow; the staff ticket page uses:

- `POST /api/staff/complaints/{id}/ai/analyze` — queues classification,
  sentiment, summary and a suggested reply (`integrations.ai_call`);
- `GET /api/staff/integration-jobs/{job_id}` — polls the queued job;
- `GET /api/staff/complaints/{id}/ai/insights` — lists decrypted insights for
  staff who can read the complaint;
- `POST /api/staff/complaints/{id}/ai/insights/{insight_id}/review` — records
  human acceptance/rejection;
- `POST /api/staff/complaints/{id}/ai/insights/{insight_id}/send` — sends only
  an accepted reply through the durable channel queue.

Complaint text is PII-minimized before provider analysis. Insight payloads are
AES-256-GCM encrypted with a job-bound associated-data value and remain
`suggested` until a staff member reviews them; replies are never auto-sent.
The deterministic mock provider is available for local/test environments, and
an OpenAI-compatible provider can be configured through the Phase 4 integration
administration APIs.

## Notification preferences

Customer email/WhatsApp/SMS preferences now reach delivery. Migration
`20261016_19` returns preferences from `claim_notices`, the worker suppresses
notices when every channel is disabled, and `SMTPDelivery` honors the enabled
channels. A real production SMS/WhatsApp provider still has to be configured
and verified; account email always uses a real configured provider and never
pretends an unconfigured provider delivered a message.

## Remaining production blockers

These are provider/deployment items, not missing application workflows:

- live SMS/WhatsApp/AI-provider and antivirus rollout (current unconfigured
  adapters fail closed);
- vendor-specific inbound IVR/STT/TTS verification;
- a dedicated integration-administration screen for connections, webhooks,
  API keys and dead-letter jobs (the permission-guarded APIs are implemented);
- production deployment target, TLS/secrets management, observability,
  backups, load testing and independent security review.
