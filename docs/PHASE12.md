# Phase 1 (completions) + Phase 2 — Service Desk, Sales CRM, Issuing, Policies

This document maps the product specification (Omnichannel Service Desk + CRM
for insurance brokerage) onto the codebase. The platform already shipped most
of Phase 1 (see `PROJECT_BRIEF.md`); revision `20261023_39` closes the
remaining gaps and adds all of Phase 2. **CRM unification note
(revision `20261025_41`):** the sales pipeline shipped here now lives
in the platform's single CRM module — see `docs/CRM_MODULE.md`
(`crm_*` tables, `/api/crm`, `/staff/crm-module`); the non-CRM
remainder (issuing, policies, payments, Client Folder, caller lookup,
corporate imports) moved to `/api/staff/insurance-ops` +
`/staff/insurance-ops`. Paths below are updated accordingly.

## Stack

- Backend: FastAPI (async) + PostgreSQL 16 (SQLAlchemy 2 + Alembic, RLS) +
  Redis. Background jobs: the pre-existing ARQ worker (transactional outbox,
  virus-scan, channel delivery) **plus Celery + beat** (`app/celery_app.py`,
  `celery_worker`/`celery_beat` compose services) owning lead follow-ups,
  overdue installments and policy-renewal alerts. Both share Redis.
- Frontend: React + Vite (`features/crm/CrmPage.tsx`).
- Infrastructure: Docker Compose / Helm, backups + restore drills and
  monitoring already in `docs/RUNBOOK.md` / `docs/DEPLOYMENT.md`; an
  **audit-log entry is written for every CRM/policy/issuing/import write**
  (actor, action, entity, state after the change), same `audit_log` table
  the audit console reads.

## Multi-tenancy

Migration 039 adds `tenants`; the existing brokerage is the default tenant
(`00000000-0000-4000-8000-0000000000a1`). Every Phase 1/2 table carries
`tenant_id`, as does `corporate_clients`. Data model chain per spec:
corporate client → contracts/`brokerage_policies` (insurer, type, period,
premium) → insured employees (`customers.corporate_client_id`) → requests
(`complaints`).

## Phase 1 mapping

| Spec item | Implementation |
|---|---|
| Unified inbox (WhatsApp + calls + email + manual) | One request queue; intake events (`request_intake_events`), staff queue `قائمة الطلبات` |
| Ticketing: unique no., طبي/عام, priority, assignee, SLA, auto-escalation, full history | `complaints` + SLA engine + timeline (pre-existing) |
| WhatsApp via official Platform/BSP | Meta Cloud provider **and 360dialog BSP** (`WhatsApp360DialogProvider`, select with `bsp=360dialog` in the WhatsApp connection, or provider `whatsapp_360dialog`); inbound message → ticket; replies from the dashboard; full conversation stored |
| Calls: cloud hotline, auto-recording, recording↔ticket, caller ID by mobile | Twilio/Vonage voice webhooks + recording/consent flow (pre-existing) + `GET /api/staff/insurance-ops/caller-lookup?mobile=` (`app_security.caller_by_mobile`) returning employee + company + policy |
| Client Folder | `GET /api/staff/insurance-ops/client-folder/{id}` and the **ملف العميل** tab: contracts, uploaded documents, insured employees, requests by status, policies |
| Company-owner portal | Corporate portal (pre-existing): contract, open/closed counts, KPIs; `corporate_users.is_owner` distinguishes the owner from a client contact |
| Admin dashboard | Analytics (status/channel/insurer/agent, avg response, overdue) pre-existing + `GET /api/staff/insurance-ops/insurer-performance` |
| RBAC roles | `administrator` (admin), `customer_service` (موظف خدمة عملاء), `account_manager` (مدير حساب), `sales`; client contact & company owner = corporate portal users. Per-employee insurer scope: `UserCompanyScope`, enforced in repositories + RLS |
| Excel import | `POST /api/staff/insurance-ops/imports/{employees,clients}` (.xlsx/.csv, Arabic headers, per-row errors, `import_jobs` ledger, audit entry); lead import is CRM-owned: `POST /api/crm/imports` |

## Phase 2 mapping

| Spec item | Implementation |
|---|---|
| Sales CRM + customisable pipeline (جديد → … → ربح/خسارة → محوّل لعميل) | unified CRM module: `crm_pipeline_stages` (tenant data, seeded with the spec stages), `crm_leads`, drag-and-drop Kanban — see `CRM_MODULE.md` |
| Add lead anytime (manual/WhatsApp/call/email) | `LeadCreate.source` enum + create form + Excel lead import |
| Activity log (calls, meetings, notes, tasks, appointments) | `lead_activities`, lead detail panel |
| Automatic follow-ups/reminders | `next_followup_at` + Celery beat `lead_followups_due` (hourly) + due badge |
| One-click Lead → Client | `POST /leads/{id}/convert` → `app_security.convert_lead_to_client` copies contact data into `corporate_clients` |
| Pipeline/Kanban + salesperson reports + conversion rates | `GET /pipeline`, `GET /reports/sales-performance`, summary strip |
| Issuing workflow (checklist per type → internal review → insurer → tracking) | `issuing_checklist_templates` (طبي/عام seeded), `issuing_requests`, items gate the `sent_to_insurer` transition |
| Policy lifecycle: installments (شهري/ربع/نصف/كاش) | `installment_schedule` (12/4/2/1, rounding-safe) → `policy_installments`; Celery flips overdue |
| Renewal alerts | `GET /renewals?days=` + Celery beat arms `renewal_alert_sent_at` once, 30 days ahead |
| Commission ≈5% + settlements | `commission_rate` default `0.05`, `commission_settlements` (pending/partial/settled) |
| Insurer performance reports | Per-insurer requests/resolved/SLA breaches/avg resolution-hours endpoint + tab |
| AI | Existing AI (call summary, auto-classification, suggested replies, voice agent) + deterministic طبي/عام classifier fallback + smart document search `GET /document-search` |
| Integrations: insurer portals, payments (Fawry/Paymob) | Insurer sends via integration connections (pre-existing); payments: `POST /installments/{id}/payment-intent`, signed `POST /api/payments/{fawry,paymob}/webhook` → `record_gateway_payment` (idempotent). Fail-closed until merchant credentials are set in `.env` |

## Mandatory legal/operational notes (unchanged behaviour)

- Personal-mobile calls are **not** recorded server-side; callers must be
  moved to the official number/softphone (operational/client decision).
- Call recording requires caller notice + consent ("المكالمة مسجلة"); the
  platform only stores a recording with recorded consent, per Egypt PDPL
  Law 151/2020 and FRA requirements (see `docs/PRIVACY.md`).
- The WhatsApp number must be provisioned for the WhatsApp Business
  Platform — it cannot be used like the regular WhatsApp app number.

## External provisioning still required before go-live

These need client/vendor accounts and cannot be created from the repo:
WhatsApp Business Platform number + BSP (360dialog/Twilio) account, Cloud
PBX/SIP hotline number, Paymob/Fawry merchant credentials, and per-insurer
portal API access where offered. All adapters fail closed (clear
`*_unconfigured` errors) until those are supplied.
