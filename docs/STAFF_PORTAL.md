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
| `/staff/admin` | Staff invitations/actions, effective-access editing, roles, global 2FA policy and company/category/SLA/template catalog JSON editors | endpoint-specific admin permissions |
| `/staff/audit` | Audit events, access-change log and metadata-only CSV export | `audit.view` |
| `/staff/exports` | Async complaint/analytics export jobs and private downloads | `complaints.export` or `analytics.export` |

Navigation is permission-filtered from the effective access snapshot returned
by `GET /api/me`; backend permission dependencies and PostgreSQL RLS remain the
authority. The queue uses the staff WebSocket snapshot stream and invalidates
the scoped REST queries when updates arrive.

## AI insights

Migration `20261016_13` opens the previously read-only `ai_insights` table to
scoped staff writes:

- `GET /api/staff/complaints/{id}/ai-insights` — `complaints.view`
- `POST /api/staff/complaints/{id}/ai-insights/generate` — `complaints.edit`
- `PATCH /api/staff/complaints/{id}/ai-insights/{insight_id}` — `complaints.edit`

The default analyzer (`local-rules-v1`) is deterministic and local: no complaint
text is sent to an external model. It produces bilingual classification,
sentiment, extractive summary and reply suggestions. Payloads are AES-256-GCM
encrypted JSON, marked `requires_human_review`, and reply suggestions are never
auto-sent. Accepting/rejecting an insight records the reviewing staff member;
a suggested reply can be copied into the public-reply editor for manual review.
Generating sentiment also updates the complaint's scoped `sentiment` field.

## Notification preferences

Customer email/WhatsApp/SMS preferences now reach delivery. Migration
`20261016_13` returns preferences from `claim_notices`, the worker suppresses
notices when every channel is disabled, and `SMTPDelivery` honors the enabled
channels. A real production SMS/WhatsApp provider still has to implement the
existing `Delivery` protocol; development keeps the explicit Mailpit mailbox
behavior and never pretends an unconfigured provider delivered a message.

## Remaining production blockers

These are provider/deployment items, not missing application workflows:

- real SMS/WhatsApp and antivirus providers (current adapters fail closed);
- business-hours calendar SLA calculation (continuous SLA is implemented and
  nonempty calendars are rejected rather than silently miscomputed);
- scheduled report delivery and materialized analytics views;
- AI voice calls (the permission/catalog foundation exists; no telephony
  provider is configured);
- production deployment target, TLS/secrets management, observability,
  backups, load testing and independent security review.
