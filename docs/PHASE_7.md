# Phase 7 — Broker dashboard and Admin console

**Product authority:** [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). Phase 7 adds a staff-facing, Arabic-first workspace over the existing FastAPI permissions and complaint-scope model. It does not replace previous migrations or customer/auth APIs.

## Frontend routes

| Route | Screen | Access requirement |
|---|---|---|
| `/staff/overview` | KPIs, time-series and Recharts analysis, scoped company/type filters, scorecards and refresh | `analytics.view` + `complaints.view` |
| `/staff/complaints` | Server-filtered queue, search, saved views, sort/page controls, bulk updates, columns, CSV and print | `complaints.view`; export/print additionally require `complaints.export` |
| `/staff/complaints/{id}` | Complaint details, timeline/messages/audit, status, assignment, reply/note, attachments, duplicate review and escalation | `complaints.view`; every action and sensitive attachment flow has its own permission/API gate |
| `/staff/users` | Directory/search/status, invitations, last login, account/2FA/session actions and access editor | `users.view`; individual actions require their `users.*` permission |
| `/staff/roles` | Custom role create/clone/edit, grouped permission matrix, protected system roles and user counts | `roles.manage` |
| `/staff/operations` | Companies, categories, SLA, templates, teams, assignment rules, integration configuration and report definitions | A matching admin permission for each tab |
| `/staff/audit` | Date-bounded event and access-change logs, ID/action filters and CSV exports | `audit.view`; access changes also require `users.manage_access` and global scope |

Navigation, direct-page access and action visibility use the effective `/api/me` access snapshot. The browser refreshes it every 30 seconds and immediately after a permission-denied API response. This is convenience, **not** the security boundary: each API request still checks permissions, effective scope and PostgreSQL RLS. Direct ticket URLs receive the same detail authorization as list selections. Company/type/agent filter options are scope-limited at the API.

## Delivered behavior

- A shared date-range picker supports 7/30/90-day and year presets plus custom dates; the selected window drives the dashboard, complaint queue and audit filter. Dashboard refresh is manual and periodic. Its KPIs and analysis include status, company, type, agent, resolution distribution, weekday/hour heatmap, recurring categories and scorecards.
- Complaint search, filters, pagination and sorting remain server-side. Saved views store criteria, not rows, and re-run under current permissions. CSV is a server-authorized export with a UTF-8 BOM for spreadsheet compatibility. Browser print is gated by export permission and is the PDF path; native XLSX is intentionally not shipped (the candidate library had unresolved high-severity advisories).
- Ticket attachments use the existing upload reservation protocol: up to five PDF/JPEG/PNG files, client-side size/checksum validation, short-lived one-use upload token, signed same-origin content upload, pending scan status and server-authorized clean-only download. The front end never marks a file safe itself.
- Access editing covers roles, Allow/Deny/Inherit overrides, company/type/ownership/team scope, live calculated effective-permission preview and a clearly labeled read-only “view as” explanation. Saving permissions/role changes uses the existing audited server operations and session invalidation.
- Operations forms call guarded catalog/automation APIs. Integration settings accept only vault references; they never return stored credentials. Scheduled reports are owner-bound definitions. Audit exports are rate-limited, formula-safe CSV and access-log export is global-scope only.
- Notifications, command palette (`Ctrl/Cmd+K`), shortcut help, theme toggle, RTL layout and responsive styles are included. Feature pages are lazy-loaded to keep the first app bundle smaller.

## Explicit non-delivery / fail-closed UI

Email escalation queues a notice but does not claim delivery. The integration configuration API stores a `vault://` reference only; email/WhatsApp runtime adapters are not configured (`runtime_ready=false`). WhatsApp sending, AI-generated summaries/replies and AI voice calling/call transcripts remain unavailable until providers and their guarded APIs are implemented. The UI labels these unavailable actions instead of fabricating results. Report schedules are saved definitions only (`delivery_ready=false`); no scheduler is present. Local file uploads remain pending and cannot be downloaded until an antivirus worker supplies a clean verdict. The separate customer portal UI is not part of this Phase 7 staff console.

## Validation

- Frontend: `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- Playwright: `npx playwright install chromium && npm run test:e2e`. Tests cover one-company visibility through mocked list/search/chart/export/direct-URL requests and a mocked live 403 permission-revocation response. GitHub CI installs Chromium and runs these tests.
- Backend: `ruff check backend scripts`, `ruff format --check backend scripts`, `mypy app`, and `pytest -m 'not integration'`. Testcontainers additionally checks real PostgreSQL/RLS behaviors: scoped list/filter-options/search/export/direct detail, analytics company scorecards, role invalidation and a revoked export grant. Run with Docker using `make test-integration` or the PostgreSQL CI job.
- All numbered migration SQL (`001_schema.sql`–`012_staff_console.sql`) was applied in PGlite in this development session; the Phase 7 security-definer smoke test verified team/assignment/report/integration functions, safe `admin_integrations()` status and denial of direct `app_runtime` table access. PGlite is **not** a substitute for PostgreSQL. The local sandbox has no Docker/PostgreSQL executable and could not download Chromium, so local Playwright and real PostgreSQL integration execution remain unverified; CI must pass before deployment.
