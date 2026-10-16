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
| `/staff/storage` | First-party, read-only inventory of the configured private S3-compatible bucket | `storage.manage` and global scope |
| `/staff/exports` | Async complaint/analytics export jobs and private downloads | `complaints.export` or `analytics.export` |

Navigation is permission-filtered from the effective access snapshot returned
by `GET /api/me`; backend permission dependencies and PostgreSQL RLS remain the
authority. The queue uses the staff WebSocket snapshot stream and invalidates
the scoped REST queries when updates arrive.

The storage route is a first-party page in the same product shell, not an
embedded MinIO Console or external open-source dashboard. It lists metadata
from the one configured S3-compatible bucket, requires `storage.manage` plus
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
and verified; development keeps the explicit Mailpit mailbox behavior and never
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
