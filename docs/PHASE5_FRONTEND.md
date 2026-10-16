# Phase 5 — Frontend foundation and public experience

Phase 5 builds the first usable Arabic-first client UI on top of the implemented `/api/*` backend. `PROJECT_BRIEF.md` remains the source of product requirements; this document records what is delivered and the integration boundaries.

## Implemented

- Vite, React 19, strict TypeScript, Tailwind v4, React Router, TanStack Query, Zustand, i18next, Framer Motion, Sonner, and generated OpenAPI types (`npm run api:types`).
- Same-origin typed OpenAPI client plus a credentialed fetch helper. Mutating requests attach the readable CSRF cookie; a shared refresh request retries one failed request after a 401. No browser code calls a sandbox-local API host.
- Arabic default locale, RTL document direction and English locale resources; Cairo/Tajawal/Inter font stack; persistent dark/light preference; logical start/end layout patterns.
- `/api/me` effective-access load after sign-in, `Can`, `usePermission`, protected staff dashboard route, permission-hidden navigation/actions, accessible 403/404 and error boundary. Access snapshots are refreshed in the background and on cross-tab access-change signals; they are UI hints only and do not replace server authorization.
- Public SaaS landing page with hero, product overview, workflow, feature/security sections, product metrics (illustrative, not measured customer claims), testimonial examples explicitly marked illustrative, FAQ, customer complaint CTAs, and a demo lead form UI.
- Staff login and second-factor challenge, customer OTP start/verify, invitation acceptance, customer complaint intake, and guest ticket tracking screens.
- Reusable visual primitives for buttons, inputs, cards, selects, multi-select, date range, files, stepper, statuses, timeline, skeleton, empty state, table, stat card, dialog/drawer, chart wrapper, and permission matrix; responsive composition and reduced-motion support.
- Installable-web-app manifest, SVG icon and a conservative offline shell service worker. API responses are never cached.
- Tests for landing/CTAs and permission gating.

## Local validation

From `frontend/`: `npm ci`, `npm run api:types` (after backend OpenAPI export if needed), `npm run lint`, `npm run typecheck`, `npm run test`, and `npm run build`.

## Integration boundaries / next work

The implemented backend contract has no `/api/demo-requests` endpoint, so the landing demo form validates client-side and reports the server error instead of pretending a lead was stored. Add a rate-limited, consent-aware server-side lead workflow and configured recipient before production. The customer complaint attachment picker is present but secure plan/upload/finalize flow is not yet connected; selected files are clearly described as not uploaded. Complaint intake requires an authenticated customer and valid matching identity as enforced by the API. The backend also does not currently expose live push notifications or a business-hours SLA calendar; the UI must not imply otherwise.

The public landing contains Arabic editorial copy beyond the translation dictionary; complete English copy translation and professional content review remain before an English-language launch. The current staff dashboard is a permission-aware starter workspace rather than the complete staff feature suite. Configure real SMS/antivirus providers, verify invitation/MFA paths in an integrated deployment, add end-to-end browser tests and accessibility audits, and connect real analytics/notifications before production.
