# Broker Hub UI parity — 16-screen audit

**State:** in progress. This is an evidence ledger, not a claim of pixel parity. The references are extracted from the supplied 16-page PDF. Source/rendered reference: [`reference/`](reference/). Product screenshots at 1440×900 and 390×844 have **not** been captured yet; no manual RTL, focus, overflow or cross-screen parity pass has been signed off.

## Reference-to-implementation matrix

| # | Reference screen | Reference | Route / implementation | API surface | State and known gap |
|---:|---|---|---|---|---|
| 1 | لوحة التحكم / dashboard | [ref-01](reference/ref-01.png) | `/staff/overview` — `features/dashboard/OverviewPage.tsx` | `/api/staff/analytics/overview`, complaint filters | **Partial.** A dashboard exists, but the reference is a broker request/lead snapshot; confirm KPI semantics, charts and latest-requests layout against real authorized data. |
| 2 | صندوق الطلبات, three-column detail | [ref-02](reference/ref-02.png) | `/staff/complaints` and `/staff/complaints/{ticket_id}` — `ComplaintQueue` + `TicketWorkspace` | `/api/staff/complaints`, ticket detail, timeline, reply/status APIs | **Partial.** Queue and detail workspace exist. Three-column 1440×900 behavior, collapsed 76px rail, audio/message area and small-screen behavior remain unverified. |
| 3 | بائع — mobile Leads | [ref-03](reference/ref-03.png) | `/staff/crm-module/pipeline` at 390×844 — responsive `StaffConsole` + `CrmModulePage` | `/api/crm/pipeline`, `/api/crm/stats`, `/api/crm/leads`, stages/tasks | **Partial.** Mobile seller view and self-scoping are implemented. Exact card density, sticky bottom navigation, overflow and seller A/B behavior need browser evidence. |
| 4 | ملف العميل | [ref-04](reference/ref-04.png) | `/staff/insurance-ops` → **ملف العميل** tab | `/api/staff/corporate`, `/api/staff/insurance-ops/client-folder/{id}` | **Partial.** Client folder and contract/request/policy data exist inside a tab; there is no standalone deep-link route matching the reference. |
| 5 | Leads Kanban | [ref-05](reference/ref-05.png) | `/staff/crm-module/pipeline` | `/api/crm/stages`, `/api/crm/pipeline`, lead move/assign/activity APIs | **Partial.** Kanban is implemented; compare stage columns/cards, KPI strip, assignment affordances and responsive width to the reference after screenshot capture. |
| 6 | بوابة الشركة | [ref-06](reference/ref-06.png) | `/corporate` — `features/corporate/CorporatePortal.tsx` | `/api/corporate/*`, `/api/company/*` | **Partial.** Authenticated corporate portal exists; reference-summary cards, document download list and typography need visual/route-flow verification. |
| 7 | فريق المبيعات | [ref-07](reference/ref-07.png) | `/staff/crm-module/performance`; reassignment from pipeline | `/api/crm/reports/sales-performance`, `/api/crm/leads/bulk-assign` | **Partial.** Performance tab, manager-only UI visibility and assignment controls exist; API self-scopes sales-role requests and manager assignment writes an activity/audit record. It is not the reference's dedicated team-card layout; integration/audit and visual checks remain. |
| 8 | تفاصيل Lead | [ref-08](reference/ref-08.png) | `/staff/crm-module/pipeline` → select a lead (detail modal) | `/api/crm/leads/{id}`, activities, tasks, convert | **Partial.** Details/activity/tasks and conversion are present in the CRM flow. No standalone URL; compare timeline, follow-up and conversion confirmation visually. |
| 9 | الموظفون المؤمّن عليهم | [ref-09](reference/ref-09.png) | `/staff/insurance-ops` → import employees / client folder employee section | `/api/staff/insurance-ops/imports/employees`, client-folder API | **Partial.** Employee import and a folder employee list exist; the reference's dedicated insured-members table, filter and row actions are not established as a separate screen. |
| 10 | إنشاء Lead | [ref-10](reference/ref-10.png) | `/staff/crm-module/leads` | `/api/crm/leads`, duplicate-phone lookup/list filter | **Partial.** Lead form exists. Validate company/person modes, duplicate-phone warning, follow-up, sales assignment permissions and save/route behavior end to end. |
| 11 | العملاء + بانر التحويل | [ref-11](reference/ref-11.png) | `/staff/customers` — `features/admin/CustomersPage.tsx` | `/api/staff/corporate`, `/api/staff/corporate/clients` | **Partial.** Client directory exists; verify conversion banner, source label and open-file navigation. **Access decision:** keep the existing `users.view` permission gate for the staff customer directory; `crm.view` alone does not grant it. This PR does not expand seller access or change the permission model. |
| 12 | الوثائق والأقساط | [ref-12](reference/ref-12.png) | `/staff/insurance-ops` → **الوثائق والأقساط** tab | `/api/staff/insurance-ops/policies`, renewals, installments, payment | **Partial.** Policy/installment and renewal data/actions exist; no visual proof of the reference's summary cards, split table/detail and status treatments. |
| 13 | إصدار وثيقة جديدة | [ref-13](reference/ref-13.png) | `/staff/insurance-ops` → **الإصدار** tab | `/api/staff/insurance-ops/issuing`, issuing checklist/status APIs | **Partial.** Issuance workflow/checklist exists; exact five-step progress, document checklist and summary composition are not matched/verified. |
| 14 | التقارير | [ref-14](reference/ref-14.png) | `/staff/reports`; insurer metrics also `/staff/insurance-ops` → **تقارير الأداء** | `/api/staff/analytics/overview`, `/api/staff/insurance-ops/insurer-performance` | **Partial.** Reports and insurer performance exist in separate areas. Reference's 30-day series and company-performance table need layout/metric mapping confirmation. |
| 15 | المستخدمون والصلاحيات | [ref-15](reference/ref-15.png) | `/staff/users` + `/staff/roles` / user access editor | `/api/staff/users`, `/api/staff/users/{id}/access`, admin access options | **Partial.** User directory and access editor exist. The reference combines them; confirm sensitive team/user operations remain hidden and server-protected for unauthorized roles. |
| 16 | تسجيل الدخول | [ref-16](reference/ref-16.png) | `/staff/login`; corporate login at `/corporate/login` | `/api/auth/*`, `/api/corporate/auth/*` | **Partial.** Login routes exist. Compare split hero/form composition and verify OTP/TOTP/forgot-password flows; no screenshot or manual authentication pass yet. |

## Tokens / fixed constraints

- Primary `#4F46E5`; sidebar `#0E1424` (232px expanded, 76px collapsed in the request workspace); background `#F3F4F9`; cards radius `18px`; font **IBM Plex Sans Arabic**.
- No schema/database changes and no business-rule changes are included. CRM changes are server-side permission/ownership enforcement and scoped reporting only.
- No production screen should be populated from invented demo records. Any future QA capture with mocked network fixtures must be labeled as fixture-rendered, not production evidence.

## Security work in this pass

- CRM sales role (`crm.manage` without `crm.convert`) is self-scoped independently of broad company scope. This applies at the API boundary to lead list/detail/update/move/activity/convert/bulk operations, pipeline and stats/KPI, tasks, imports and sales-performance reporting.
- Import creation is assigned to the seller; import batches and row-error exports are limited to the uploader. Task counts join through the lead owner, not the task assignee.
- Stage counts are seller-scoped and stage-definition mutation is manager-only. Manager reassignment remains available and writes an assignment activity record.
- This is covered by backend unit tests for seller-vs-other-owner denial, manager access/reassignment, list/Kanban self filters, stage count scope and stage-admin denial. Full multi-user database integration and manual authorization checks remain outstanding.

## Verification record

| Check | Result | Notes |
|---|---|---|
| Frontend production build | Pass | `npm run build`; Vite warns that the main JS chunk exceeds 500 kB. |
| Frontend lint | Pass, zero warnings | `npm run lint`; Fast Refresh barrel/hook cases are explicitly documented and locally suppressed. |
| Frontend unit tests | Pass, 216/216 | `npm test -- --run`. |
| Backend non-integration suite | Pass, 615 passed / 100 deselected | Includes CRM security tests. Four third-party deprecation warnings. |
| Ruff | Pass | `ruff check backend scripts loadtest`; format check passes (250 files). |
| Browser E2E / actual captures | **Blocked** | Playwright attempted 24 specs, but Chromium executable was missing so none launched. No application screenshots under `screenshots/` yet. |
| Manual critical flows | **Not verified** | Login, create/duplicate Lead, assignment/reassignment, conversion→client banner/file, reply/status change, client folder and company portal. |

## Remaining before PR #58 can leave Draft

1. Capture and review 1440×900 desktop and 390×844 salesperson screenshots for all applicable views; check RTL, focus states, horizontal overflow, the collapsed inbox rail and actual API-backed data.
2. Fill each row above with observed parity deviations and decide which missing routes (insured members, team view, lead detail deep-link, client-folder deep-link) must be implemented to meet the PDF.
3. Run the critical end-to-end flows, especially two salespeople A/B and manager reassignment/audit trail, against an isolated backend; test customer-directory permissions and company portal access.
4. Review and disposition the >500 kB main-chunk warning deliberately; update the PR description with screenshots, routes, APIs, permissions, results and deviations after captures exist.
5. Keep PR #58 Draft until P0/P1 screens, screenshots and checks are complete. Do not merge to `main`.
