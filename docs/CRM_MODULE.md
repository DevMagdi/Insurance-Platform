# Isolated CRM module (`/api/crm`, `/staff/crm-module`)

A small, self-contained CRM dedicated to lead capture and Excel lead
import. It deliberately ships its own isolated tables with the `crm_`
prefix and does not touch the complaint/service-desk schema or the B2B
Sales CRM at `/api/staff/crm` (see `docs/SERVICE_DESK_PHASE2.md`).

## Database (revision `20261024_40`)

| Table | Purpose |
| --- | --- |
| `crm_leads` | `full_name`, `phone`, `email`, `source`, `status` (`new/contacted/qualified/converted/lost`), `assigned_to` → `staff_users`, `notes`, `import_batch_id` → batches, timestamps, `tenant_id` (multi-tenancy from day one). |
| `crm_import_batches` | one row per upload: `file_name`, `uploaded_by`, `total/success/failed rows`, derived `status`. |
| `crm_import_errors` | per-row failures: `batch_id`, 1-based `row_number` (header = row 1), stable `reason` code, `raw_data` JSONB. |
| `crm_activities` | lead timeline: `lead_id`, `type`, `description`, `created_by`, `created_at`. |

Deduplication: partial unique indexes `(tenant_id, phone)` and
`(tenant_id, lower(email))`; indexes on `status`, `source`, `assigned_to`,
`created_at`, `full_name`. Staff-actor RLS like every platform table.
Fully reversible: the downgrade drops only the four `crm_*` tables.

```bash
cd backend && alembic upgrade head      # apply
cd backend && alembic downgrade -1      # remove only crm_* tables
```

## Excel import format

`.xlsx` (openpyxl) / legacy `.xls` (`xlrd>=2,<3`) / `.csv`, max 5 MB,
first sheet, header on row 1. Headers are matched case-insensitively in
English and Arabic:

| Field | Accepted headers (examples) |
| --- | --- |
| `full_name` (required) | `full_name`, `name`, `الاسم`, `اسم العميل` |
| `phone` | `phone`, `mobile`, `الهاتف`, `رقم الهاتف`, `الموبايل` |
| `email` | `email`, `e-mail`, `البريد`, `البريد الإلكتروني` |
| `source` | `source`, `المصدر` — free text folded onto `manual/whatsapp/call/email/portal/other`, default `import` |
| `notes` | `notes`, `note`, `ملاحظات` |

Egyptian numbers starting `0` fold to `+20…`; emails are lower-cased.
Invalid and duplicate rows (within the file or already in the database)
are recorded in `crm_import_errors` with reasons `missing_full_name`,
`invalid_phone`, `invalid_email`, `missing_contact`, `duplicate_phone`,
`duplicate_email` — the rest still import, in one transaction per file.
Downloadable sample: `GET /api/crm/imports/template.xlsx`. File size and
extension are validated server-side and spreadsheet content is never
executed or trusted (read-only parsing only).

## API (staff session; `crm.view` reads, `crm.manage` writes)

- `GET/POST /api/crm/leads` — search, filter (`status`, `source`,
  `assigned_to`, `date_from`, `date_to`), sort (`created_at`,
  `full_name`, `status`, `source`), pagination.
- `GET/PATCH/DELETE /api/crm/leads/{id}`, `POST …/activities`,
  `POST /leads/bulk-assign`, `POST /leads/bulk-status`.
- `POST /api/crm/imports` + `POST /api/crm/imports/preview`,
  `GET /api/crm/imports`, `GET /api/crm/imports/{id}`,
  `GET /api/crm/imports/{id}/errors.csv`, `GET /api/crm/imports/template.xlsx`.
- `GET /api/crm/stats` — totals, by status/source, last-30-days series
  (zero-filled), conversion rate, recent leads/imports/activities.

Every write requires the CSRF token and writes an audit row
(`crm_lead_created/updated/deleted/status_changed/assigned`,
`crm_import_completed`). Permissions already exist and are seeded onto
the sales roles (`crm.view`/`crm.manage`, revision 039) — no new seed or
environment variable is required (`xlrd` is the only new dependency).

## Frontend

`/staff/crm-module` with sub-tabs `dashboard | leads | import | history`
(permission-gated expandable "CRM" sidebar group): Recharts dashboard,
searchable/sortable/bulk-action leads table with a detail drawer
(activity timeline), drag-and-drop import with preview and upload
progress, and import history with per-batch error drill-down and CSV
error report download.
