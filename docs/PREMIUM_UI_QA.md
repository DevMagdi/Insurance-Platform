# Premium UI polish — validation

## Scope

Frontend only; no API, authorization, routing, persistence, dependency manifest,
or backend changes. Framer Motion, TanStack Query and state management remain.
Work uses Arena's fixed feature branch `arena/ae421a2b-insurance-platform`.

- Shared semantic surface, text, border, accent and feedback tokens, including
  independently scoped staff light/dark themes (regardless of customer theme).
- Existing brand utility names preserved; semantic Tailwind utilities added.
- Arabic fonts, softened dark ink, off-white canvas, layered elevation, consistent
  control/card geometry, focus and reduced-motion support.
- Token-based shared input, accessible skeleton loading, notification styling,
  status transitions and subtle timeline entry motion.
- Landing spacing, wizard/review hierarchy, ticket list rhythm, conversation
  bubbles, attachment/dropzone focus states.
- Staff navigation palette, readable table density, conversation/context columns,
  analytics cards and restrained admin KPI/chart surfaces.

## Local checks

All passed on the final implementation:

| Check | Result |
| --- | --- |
| `npm run typecheck` | Pass |
| `npm run lint` | Pass; 2 existing Fast Refresh warnings in Branding.tsx and ui.tsx |
| `npm run test` | 34 files / 165 tests passed |
| `npm run build` | Pass; existing large-chunk advisory remains |
| `npm run test:e2e` | 48 tests passed, Chromium mobile + desktop |

The sandbox has no installed browser/system NSS libraries and restricts browser
CDN access. Browser verification used a temporary, untracked npm-distributed
Chromium executable plus its bundled shared libraries. No project dependencies
or lockfile changes were needed. Local E2E invocation:

```sh
LD_LIBRARY_PATH=/tmp/chromium-libs/lib \
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/tmp/chromium npm run test:e2e
```

CI uses the existing Playwright browser installation, without that override.

## Browser coverage (network-mocked API, real React/Vite app)

- Existing full wizard submission including OTP and mandatory consent, tickets
  navigation, tracking, staff login/TOTP and queue → workspace all pass on mobile
  and desktop.
- Added AR/RTL and EN/LTR customer route matrix: landing → wizard entry → tickets
  → detail, in both themes; checks visible headings, overflow, timeline,
  conversation and resolved semantic tokens.
- Added Arabic staff theme matrix with the *opposite* customer/root theme:
  queue → workspace → analytics → admin, both themes and both viewports. Checks
  resolved theme colors, overflow, KPIs and chart rendering.
- Reduced-motion browser preference exercised in the new matrix.
- Existing permission/scope, revocation, access-editor and onboarding tests pass.
- Rendered screenshot review: Arabic dark customer detail, English light mobile
  detail, Arabic light desktop staff workspace, Arabic dark admin dashboard.
  Playwright writes customer/staff/admin matrix screenshots to ignored
  `frontend/test-results`; they are not committed.

This is not a live-production/backend manual verification or an exhaustive
screen-reader/contrast audit. Existing Arabic-only staff console copy is retained;
this change does not claim to translate that pre-existing console.
