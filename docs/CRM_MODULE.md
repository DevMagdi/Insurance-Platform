# CRM — the single source of truth (`/api/crm`, `/staff/crm-module`)

The platform has exactly **one** CRM: the unified module in this
document. Migration `20261025_41` folded the former B2B sales pipeline
(`/api/staff/crm`, tables `leads` / `pipeline_stages` /
`lead_activities`) into the `crm_*` tables and **dropped** the old
tables and router. Insurance operations that used to share that router
(issuing, brokerage policies/installments/commissions, payments,
Client Folder, caller lookup, corporate Excel imports) are *not* CRM
and now live under `/api/staff/insurance-ops` + `/staff/insurance-ops`
(الوثائق والإصدار); issuing requests may still reference a CRM lead
(`issuing_requests.lead_id → crm_leads`).

## Scope

Leads (manual/WhatsApp/call/email/portal/import) with a
tenant-customisable pipeline, drag-and-drop Kanban, scheduled tasks &
follow-ups with Celery reminders, one-click Lead → corporate client
conversion (Client Folder), salesperson performance reports, Excel
lead import, and a CRM dashboard — all multi-tenant (`tenant_id` on
every table), RLS-enforced, permission-gated (`crm.view` reads,
`crm.manage` writes, `crm.convert` conversion) and audit-logged on
every write.

The seeded `sales` role is self-scoped at the API boundary: lead-list and
Kanban queries are forcibly restricted to `assigned_to = current staff user`,
and fetching another salesperson's lead returns 404. Dashboard totals,
recent leads, activities, and follow-up counts use that same assignment
scope. Team-capable roles retain their existing team views. UI filters are
not treated as an authorization boundary.

## Database (`crm_*` only)

| Table | Purpose |
| --- | --- |
| `crm_pipeline_stages` | Tenant pipeline stages (`code`, `name_ar/en`, `sort_order`, mutually exclusive `is_won/is_lost/is_converted`, `is_active`). Seeded defaults: جديد → تم التواصل → في ميتنج → يحتاج معلومات/عرض سعر → عرض مرسل → تفاوض → ربح → خسارة → محوّل لعميل (`new, contacted, meeting, needs_info, quote_sent, negotiation, won, lost, converted`). |
| `crm_leads` | `lead_no` (unique), `full_name`, optional `company_name`, `phone`, `email`, `source`, `stage_code` (→ stage code, no DDL needed to customise), `priority` (low/normal/high/urgent), `insurance_type`, `insurer_company_id`, `expected_premium`, `assigned_to`, `notes`, `lost_reason`, `converted_corporate_client_id` + `converted_at`, `closed_at` (stamped from the stage's terminal flag), `next_followup_at` (earliest open task), `import_batch_id`, `created_by`, timestamps. Dedup: partial unique `(tenant_id, phone)` and `(tenant_id, lower(email))`. |
| `crm_tasks` | Scheduled work: `lead_id`, `type` (call/meeting/email/whatsapp/task), `title`, `description`, `due_at`, `status` (pending/done/cancelled), `assigned_to`, `completed_at`, `reminder_sent_at` (Celery beat). |
| `crm_activities` | The lead timeline (`call/note/stage_change/task/conversion/whatsapp/email/meeting/assignment`); stage moves, assignments, task lifecycle and conversion are auto-logged. `task_id` links task-produced entries. |
| `crm_import_batches` / `crm_import_errors` | Excel import ledger with per-row error reasons (`missing_full_name`, `invalid_phone`, `invalid_email`, `missing_contact`, `duplicate_phone`, `duplicate_email`) and raw row JSONB. |

Stage deletion is data-protected: a stage holding leads returns 409
(deactivate instead), and the intake stage (`new`) cannot be deleted
or deactivated. Stage rows are per-tenant; `POST /stages/reorder`
takes the full ordered id list.

### Migrations

- `20261024_40` introduced the `crm_*` import module (expand-only).
- `20261025_41` is a deliberate **contract** migration (pure Python,
  not an expand-only `.sql` revision): it creates
  `crm_pipeline_stages`/`crm_tasks`, widens `crm_leads`, copies
  `pipeline_stages`/`leads`/`lead_activities` preserving ids (phone/
  email collisions against the dedup indexes are nulled, never
  dropping a lead), repoints `issuing_requests.lead_id`, replaces
  `app_security.convert_lead_to_client`, then drops the old tables.
  **Do not run the old binary against a 041-migrated database.**
  The downgrade recreates the 039 tables and copies back best-effort.

```bash
cd backend && alembic upgrade head      # apply (includes 041)
cd backend && alembic downgrade -1      # restore the split 039 shape
```

## API (`/api/crm`, staff session)

| Area | Endpoints |
| --- | --- |
| Stages | `GET /stages` (+`?include_inactive=true`, each with lead `count`), `POST /stages`, `PATCH /stages/{id}`, `DELETE /stages/{id}` (409 when non-empty), `POST /stages/reorder` |
| Leads | `GET /leads` (search incl. company/`lead_no`, `stage_code`, `source`, `priority`, `assigned_to`, date range, `due_before`, sort, pagination), `POST /leads`, `GET /leads/{id}` (activities + tasks), `PATCH`, `DELETE`, `POST /leads/{id}/move` (Kanban drop; stamps `closed_at`), `POST /leads/bulk-assign`, `POST /leads/bulk-stage`, `POST /leads/{id}/activities`, `POST /leads/{id}/convert` (`crm.convert`; one-click corporate client via the definer function, stage becomes `converted`) |
| Tasks | `GET /tasks` (`status`, `type`, `assigned_to`, `lead_id`, `due=overdue\|today\|upcoming` + `due_counts`), `POST /leads/{id}/tasks`, `PATCH /tasks/{id}`, `POST /tasks/{id}/complete`, `DELETE /tasks/{id}` — every change recomputes the lead's `next_followup_at` |
| Kanban | `GET /pipeline` (stages with counts, per-stage value and cards; `assigned_to`/`source`/date filters; totals, win/conversion rates, open pipeline value) |
| Performance | `GET /reports/sales-performance?date_from=&date_to=&source=&stage_code=` — per salesperson: leads, won/lost/converted, conversion & win rates, won/pipeline value, average close days, tasks done/overdue. Staff without global company scope see only their own row. |
| Dashboard | `GET /stats` — totals, `by_stage` (with stage names), by source, zero-filled last-30-days, conversion rate, due-task counts, top performers, recent leads/imports/activities. |
| Import | `POST /imports` (+`/imports/preview`), `GET /imports`, `GET /imports/{id}`, `GET /imports/{id}/errors.csv`, `GET /imports/template.xlsx` — see "Excel import". |

Audit actions: `crm_lead_*`, `crm_stage_*`, `crm_task_*`,
`crm_import_completed` (resource types `crm_lead`,
`crm_pipeline_stage`, `crm_task`, `crm_import_batch`).

## Tasks, follow-ups and reminders

Completing/cancelling/rescheduling a task recomputes
`crm_leads.next_followup_at` (earliest open `due_at`), which drives the
due badges in the UI. The Celery beat job
`app.celery_app.crm_task_reminders` (hourly, `app_worker` scope with
`crm_tasks_worker`/`crm_leads_worker` RLS policies) stamps
`reminder_sent_at` once per due pending task and reports how many
leads await follow-up.

## Excel import format

`.xlsx` / legacy `.xls` / `.csv`, max 5 MB, header on row 1, matched
case-insensitively in English and Arabic: `full_name` (`الاسم`,
`اسم العميل`), `phone` (`الهاتف`, `الموبايل`), `email` (`البريد`),
`source` (`المصدر` — folded onto the source vocabulary, default
`import`), `notes` (`ملاحظات`). Egyptian `01…` numbers fold to `+20…`;
emails lower-case; imported leads enter stage `new` with a generated
`lead_no`. Invalid/duplicate rows land in `crm_import_errors` (the
rest still import, one transaction per file). Spreadsheet content is
parsed read-only and never trusted.

## Frontend (`/staff/crm-module`)

A single collapsible **CRM** sidebar group (hidden without
`crm.view`) with tabs **Dashboard | Pipeline (Kanban) | Leads | Tasks |
Import | Performance | History**: Recharts dashboard (by stage/source,
30-day series, overdue badges, top performers); drag-and-drop Kanban
with filters and a stage manager (add/edit/reorder, protected delete);
searchable leads table with bulk assign/stage and a detail drawer
(stage/priority/premium editing, tasks, timeline, one-click convert);
a global tasks board with overdue/today/upcoming badges; salesperson
performance (chart + table with average close time); drag-and-drop
Excel import with preview/progress/error report; import history.

## Code map

- Backend: `app/api/crm_module.py`, `app/repositories/crm_module.py`,
  `app/models/crm_module.py`, `app/domain/crm_module_schemas.py`,
  `app/services/crm_import.py` (parse/map/dedupe),
  `app/services/crm_pipeline.py` (default stages, terminal semantics),
  `app/services/tabular.py` (shared spreadsheet parser).
- Frontend: `frontend/src/features/crm-module/`.
- Tests: `backend/tests/test_crm_module.py`,
  `backend/tests/test_insurance_ops(.py|_api.py)` (the renamed
  non-CRM remainder), `CrmModulePage.test.tsx`,
  `InsuranceOpsPage.test.tsx`.
