# Privacy and data protection (Egypt PDPL, Law 151/2020)

This document describes how the platform supports Egypt's Personal Data
Protection Law (Law No. 151 of 2020) and its executive regulations. It is an
engineering description, **not legal advice**. A PDPL legal review, data
protection officer (DPO) appointment, and licensing or registration with the
Personal Data Protection Center remain the operator's responsibility before
go-live.

## Data inventory

| Category | Examples | Storage | Protection |
|---|---|---|---|
| Identity (sensitive) | Egyptian national ID | `customers`, `complaints` | AES-256-GCM (versioned keyring) plus an HMAC blind index for lookup. Masked by default. Full reveal needs a permission, a reason and an audit entry. |
| Contact | mobile, e-mail, WhatsApp availability | `customers` | RLS (owner or scoped staff). Mobile is used for OTP. |
| Complaint content | subject, description, replies, attachments | partitioned `complaints` and child tables, private S3 | RLS and scope. Attachments are private, scanned, and downloaded through signed URLs only. |
| Health or financial details inside complaints | free text and files | as above | Treated as sensitive. Minimised before any AI provider call (IDs, phones, e-mails, names and policy numbers are masked). |
| Voice | call transcripts and recordings | encrypted columns, encrypted objects | Only with recorded voice consent. AES-GCM. Downloads are permission-gated and audited. |
| Technical | IP, user agent, session metadata | `auth_sessions`, consent evidence, logs | Limited retention. Logs carry no national IDs, OTPs or tokens. Redis keys are HMAC-hashed. |

## Lawful basis and consent

* **Complaint processing:** the customer explicitly ticks a consent box in the
  complaint wizard before submission (policy version
  `PRIVACY_POLICY_VERSION`, exposed at `/api/public/config`). The server
  records it.
* **Consent evidence** (`consent_records`, migration 021) is append-only.
  Each row stores the customer, purpose, granted or withdrawn, policy version,
  source (`web`, `staff`, `import`), IP and user agent, and a timestamp. The
  latest row per purpose is the current state. Writes only go through the
  `record_consent()` SECURITY DEFINER function, so no runtime role can edit
  or delete evidence.
* **Purposes:** `complaint_processing`, `notifications`, `voice_followup`,
  `ai_processing`, `marketing`. Marketing is never enabled by default.
* **Withdrawal:** `POST /api/customer/privacy/consents` with
  `granted: false`. Channel preferences and voice consent are enforced again
  by the worker at send time.

## Data-subject rights

| Right (PDPL) | Endpoint | Behaviour |
|---|---|---|
| Know / access a copy | `GET /api/customer/privacy/export` | Machine-readable JSON with the profile, the customer's own complaints (ticket, dates, type, policy, subject, description, status; up to 1,000), consent history and privacy requests. Internal notes and staff data are never included. RLS-bound to the signed-in owner. Rate-limited to 5 per hour and counted in `sensitive_actions_total{action="privacy_export"}`. |
| Rectification | `POST /api/customer/privacy/requests` with `kind: rectification` (profile fields are also editable through `/api/customer/profile`) | |
| Erasure | `POST /api/customer/privacy/requests` with `kind: erasure` | When staff complete it, the name is anonymised, e-mail and channel preferences are cleared, `token_version` is bumped and every session is revoked. Complaint records and the encrypted national ID are kept only as long as insurance-regulatory retention requires (a lawful basis under the law). |
| Objection / restriction | `kind: objection` | Handled by the DPO; usually withdraws a purpose. |
| Request status | `GET /api/customer/privacy/requests` | |

Staff handle requests via `GET /api/staff/privacy/requests` and
`POST /api/staff/privacy/requests/{id}`. These require `settings.manage` and
global scope. Staff decisions are written to `audit_logs`. Requests are due within **30 days**
(`due_at`), and only one open request per kind is allowed per customer.

## Retention schedule

| Data | Retention | Mechanism |
|---|---|---|
| Expired or revoked refresh-token families | 30 days after the last expiry or revocation (`RETENTION_SESSION_DAYS`, 7–365) | worker `apply_retention`, daily at 03:17 UTC |
| Sent, failed or suppressed notification and insurer outbox rows | 180 days (`RETENTION_OUTBOX_DAYS`, 30–3650) | same |
| Integration jobs and dead letters | kept (they are audit evidence for regulator inquiries) | admin replay or purge |
| Export files | short-lived; the `exports/` prefix expires through the worker cron and bucket lifecycle | |
| Complaints, events, audit log | per insurance-regulatory and broker licence obligations (set by legal; typically ≥ 5 years after closure) | partition detach and archive, then drop, as a reviewed operation |
| Logs | 30–90 days in the log platform | platform policy |
| Backups | 35 days PITR, 90 days encrypted off-site | see [RUNBOOK.md](RUNBOOK.md#backups-and-disaster-recovery) |

`apply_retention` only runs as `app_worker` with the worker scope, validates
its bounds, and deletes in bounded batches.

## Security of processing

See [SECURITY.md](SECURITY.md). The measures include:

* encryption at rest for sensitive fields, with key rotation;
* TLS in transit;
* RLS and scope-based least privilege, with an automated pen-test suite;
* audit trails for exports, reveals and access changes;
* rate limiting and CAPTCHA;
* backups with restore drills;
* monitoring with suspicious-access alerts.

## Cross-border transfer

AI and voice providers may process data outside Egypt. Under the PDPL,
cross-border transfer needs an adequate level of protection and a licence or
permit from the Center. Before enabling such a provider:

* record the `ai_processing` / `voice_followup` consent;
* keep PII minimisation on (the default);
* sign a data processing agreement;
* confirm that the transfer is permitted.

## Breach notification

PDPL requires notifying the Personal Data Protection Center **within 72 hours**
of becoming aware of a breach, and informing affected data subjects where
required.

1. Contain the incident ([RUNBOOK.md](RUNBOOK.md#suspicious-access)).
2. Preserve evidence: logs, an audit export, and the Sentry event IDs.
3. Assess scope with the audit trail: which customers and complaints were
   affected, and whether the data was encrypted (national IDs are encrypted at
   rest).
4. The DPO notifies the Center and affected customers. Record the decision and
   the timeline.
5. Post-incident: rotate keys if exposure is possible, and add a regression
   test to the pen-test suite.
