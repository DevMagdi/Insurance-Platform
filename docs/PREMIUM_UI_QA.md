# Premium UI polish — validation

## Scope

Frontend only; no API, authorization, routing, persistence, dependency manifest,
or backend changes. Framer Motion, TanStack Query and state management remain.
Initial pass used Arena's fixed feature branch `arena/ae421a2b-insurance-platform`; the elevation pass below used `arena/c963ec4f-insurance-platform`.

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

## Elevation pass (Wathiq design-system unification)

Frontend only; no API, authorization, routing, persistence, dependency, or
backend changes. Framer Motion, TanStack Query, i18n and state management remain.

- **Foundation**: deepened dark surfaces, token-driven scrollbars, interactive
  card + gold button refinements, unified semantic tokens (`--info/--success/
  --warning/--danger/--violet/--orange/--fuchsia` + `-soft`).
- **Staff console CSS fully tokenized** (`staff-console.css`, `staff.css`,
  admin identity/dashboard): status/priority pills now share one semantic map
  with the customer portal (new=info, assigned=violet, in_review=warning,
  resolved=success, reopened=orange, transferred=fuchsia). Auth surfaces use
  theme-invariant tokens only. Modal scrim and popover entrance motion unified
  (`staff-pop` / `staff-fade-in`).
- **Customer portal**: ambient hero glow (clipped to the hero box so it never
  widens the document), wizard step halo + choice-card lift, dropzone focus
  ring, journey gold sheen + current-node halo, mobile bottom-nav raised FAB.
  Customer pages get a reduced-motion-aware Framer Motion page transition.
- **Analytics**: Overview charts read CSS tokens (gold/emerald/info/fuchsia/
  orange/ink) with a semantic per-status palette; loading state uses
  chart-shaped skeletons instead of a spinner.
- **Standalone pages** (staff login, invite, analytics, audit exports,
  error boundary) moved from raw slate utilities to semantic tokens.
- Four unreferenced legacy components removed (verified zero imports).

### Local checks (elevation pass)

| Check | Result |
| --- | --- |
| `npm run typecheck` | Pass |
| `npm run lint` | Pass; same 2 pre-existing Fast Refresh warnings |
| `npm run test` | 34 files / 165 tests passed |
| `npm run build` | Pass |
| `npm run test:e2e` | 48/48 passed, Chromium mobile + desktop |

Screenshot review (network-mocked API, real app): landing + wizard (incl.
review/success) + tickets + detail + profile in AR/EN × light/dark; staff
queue, workspace, overview, admin in light/dark. One regression found and
fixed during review: the hero glow initially widened the document on desktop
(`scrollWidth > innerWidth`); it is now clipped to the hero box and the
gradients recentered.
