# Phase 7 — Scoped staff console and operations administration

**Product authority:** [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). Phase 7 adds a permission- and scope-driven staff console to the Phase 1–6 application. It reuses the existing authentication, access snapshot, complaint, analytics, workflow, catalog, reporting and Phase 4 integration APIs; it does not create a parallel ticket or AI workflow. Customer routes and the main AI-capable staff ticket screen remain intact.

## Frontend routes

| Route | Screen | Access requirement |
|---|---|---|
| `/staff/overview` | Scoped KPIs, trends, heatmap and company/category/agent analytics | Analytics and complaint permissions, enforced by the APIs |
| `/staff/complaints` | Searchable/filterable complaint queue, saved views, bulk actions and export | `complaints.view`; export/actions require their own permissions |
| `/staff/complaints/{id}` | Existing main staff ticket workspace, including timeline, replies/notes, attachments, assignment, escalation and human-reviewed AI assistance | Existing ticket and action permissions/scope |
| `/staff/users` | Staff directory, invitations, user status and effective-access editing | `users.view`; each mutation has its own permission |
| `/staff/roles` | Custom roles and permission catalog | `roles.manage` |
| `/staff/operations` | Company/category/SLA/template administration, teams, assignment rules, integration references and scheduled reports | Permission checks for each operation/tab |
| `/staff/audit` | Filterable event and access-change history, bounded exports | `audit.view`; access-change data additionally requires `users.manage_access` and global scope |

The console is lazy-loaded and its styles are scoped below `.staff-console`, so it does not reset the main portal's global styles or theme. A complaint UUID route deliberately continues to render the existing `StaffTicketPage`; only queue/overview/admin routes mount the new console. Customer portal and authentication routes are unchanged. Navigation and action visibility use `/api/me` as a convenience; the server remains the authorization boundary.

## Data, authorization and integrations

- Dashboard and complaint data come from the existing scoped APIs. Requests are reauthorized using the caller's current permission snapshot, `AccessScope` and PostgreSQL RLS; hiding a menu or button is not security. Direct detail, exports, analytics, bulk mutations and attachments retain their existing server-side scope checks.
- Team membership and assignment-rule changes use the Phase 7 admin repository's existing `app_security` SECURITY DEFINER functions. The UI only offers global team/automation administration when the caller has the matching permissions and global scope; team editing also requires the staff-directory permission. The migration adds only staff-console metadata/functions after main's `20261016_19` revision; it does not recreate the Phase 3 automation, catalog, analytics, report or Phase 4 integration tables.
- Integration settings in the operations screen are non-secret references through `/api/staff/admin/integrations`. Phase 4 encrypted provider connections remain separate at `/api/staff/admin/integration-connections`; credential values are never returned to the browser. AI analysis, generated insights, human approval, message sending and voice flows remain on the canonical Phase 4 APIs and worker.
- Scheduled reports use `/api/staff/reports/schedules`, with relative filters such as `window_days`. The API validates the cron expression/time zone and computes `next_run_at`; the existing worker revalidates the owner's current access and each report row when a run executes. Saving a schedule is not a claim that delivery has occurred.
- Event exports support filtered CSV and XLSX; access-change export is CSV and is global-scope-only. Exports are bounded, rate-limited, no-store, and CSV formula-safe. UI-selected date, actor, complaint, action and target filters are sent to the same export endpoints.
- Attachment operations reuse the signed reservation/upload and clean-only download flow. The browser cannot mark files safe; a configured scanner must supply a clean verdict through the worker boundary.

## Migration and contracts

The new Alembic revision is `20261016_20`, with parent `20261016_19`. Its SQL source is `backend/alembic/sql/020_staff_console.sql`. It adds team/assignment administration helpers and the required permission catalog additions while preserving the main branch's existing SQL functions and grant model. `20261016_20` is the expected Alembic head; no duplicate report-schedule, assignment-rule, integration-credential or Phase 4 AI tables are introduced.

`docs/openapi.yaml` is generated from the live FastAPI application and is checked against `app.openapi()` in the architecture tests and the published API image in CD. Cookie, CSRF, permission and WebSocket annotations cover the registered API routers, including the main branch's integration and public API routes. Regenerate it with `PYTHONPATH=backend .venv/bin/python scripts/export_openapi.py` after API contract changes.

## Verification and remaining gates

- Backend checks in this checkout: Ruff lint/format, `mypy app`, and offline tests with the configured 80% coverage floor. The full PostgreSQL/RLS/Testcontainers suite requires Docker; the sandbox does not provide a Docker daemon or PostgreSQL server, so a local emulator is not treated as a database integration pass.
- Frontend checks: lint, TypeScript typecheck, Vitest, production build and Playwright. CI installs Chromium for browser tests; local Chromium download previously failed with a TLS connection reset.
- The CI PostgreSQL job is responsible for applying every migration to real PostgreSQL 16 through PgBouncer and running the RLS/integration suite. CI/CD status must be checked on the merged `main` revision before claiming post-merge verification.
- Live provider delivery, production scanner setup, vendor-specific inbound IVR/STT/TTS, accessibility review, load testing, backup/recovery drills and PDPL/security review remain deployment work. The CD workflow publishes and smoke-tests images on `main`; deployment is separately opt-in and is not implied by a successful CI or image smoke test.
