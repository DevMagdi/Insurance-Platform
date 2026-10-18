import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * The Operations page used to blank out with a render-time TypeError
 * (`Cannot read properties of undefined (reading 'join')` on the reports row,
 * `reading 'window_days'` before it): the table cast whatever the API returned
 * straight into a record type, so one partial row — or one response that
 * arrived for a tab the operator had already left — crashed the whole page.
 *
 * Every test here therefore also fails on any uncaught page error or console
 * error. That is what catches the second bug: browsers compile an HTML
 * `pattern` attribute with the `v` (unicodeSets) flag, where an unescaped `/`
 * or `-` inside a character class throws "Invalid regular expression" and the
 * constraint is silently dropped.
 */
const adminAccess = {
  id: '44444444-4444-4444-8444-444444444444', full_name: 'Platform Admin', email: 'admin@example.test',
  status: 'active', token_version: 1, two_factor_enabled: true, two_factor_required: true,
  roles: ['admin'],
  permissions: [
    'companies.manage', 'categories.manage', 'sla.manage', 'templates.manage', 'teams.manage',
    'settings.manage', 'integrations.manage', 'reports.manage', 'analytics.view', 'analytics.export',
    'users.view', 'complaints.assign', 'complaints.view',
  ],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const completeSchedule = {
  id: 'r-1', name: 'تقرير أسبوعي', filters: { window_days: 30, company_id: null, insurance_type: null },
  cron_expression: '0 8 * * 1', time_zone: 'Africa/Cairo', recipient_emails: ['ops@example.test'],
  format: 'csv', is_active: true, next_run_at: '2026-10-19T06:00:00Z', last_run_at: null,
  last_state: null, created_at: '2026-10-01T08:00:00Z',
};
const completeCompany = {
  id: 'co-1', slug: 'nile-assurance', name_en: 'Nile Assurance', name_ar: 'تأمين النيل',
  contact_emails: ['claims@nile.test'], supported_types: ['health', 'general'], is_active: true,
};

/**
 * The signatures of the two bugs this spec guards. The filter keeps the
 * assertion honest about the failure it targets while ignoring unrelated
 * browser noise (a devtools hint, a 404 for a static asset).
 */
const FATAL_ERROR = /TypeError|is not a function|Cannot read propert|Invalid regular expression|UI boundary caught an error|Minified React error/i;
function fatalErrors(errors: string[]): string[] {
  return errors.filter((line) => FATAL_ERROR.test(line));
}
function expectCleanBrowser(errors: string[]): void {
  expect(fatalErrors(errors), `browser errors: ${errors.join(' | ') || 'none'}`).toEqual([]);
}

async function install(page: Page, data: { companies: unknown[]; schedules: unknown[] }) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  await page.addInitScript(() => {
    localStorage.setItem('insurance-portal:lang', 'ar');
    document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax';
  });
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/me') return json({ kind: 'staff', access: adminAccess });
    if (path === '/api/auth/csrf') return json({ available: true, token: 'e2e-session' });
    if (path === '/api/public/config') return json({ platform_name: 'Insurance', logo_url: '/insurance-logo.svg' });
    if (path === '/api/me/notifications') return json([]);
    if (path === '/api/staff/complaints/filter-options') {
      return json({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] });
    }
    if (path === '/api/staff/admin/companies') return json(data.companies);
    if (path === '/api/staff/reports/schedules') return json(data.schedules);
    if (path === '/api/staff/admin/automation-options') {
      return json({
        companies: [{ id: 'co-1', name_en: 'Nile Assurance', name_ar: 'تأمين النيل' }],
        categories: [{ id: 'cat-1', company_id: 'co-1', name_en: 'Delayed claim' }],
        teams: [{ id: 'team-1', name: 'فريق المتابعة' }],
        agents: [{ id: 'agent-1', full_name: 'Mona Ali' }],
      });
    }
    return json([]);
  });
  return errors;
}

test('renders complete and partial records without a single console error', async ({ page }) => {
  const errors = await install(page, {
    companies: [
      completeCompany,
      // A record the API can return partially filled (an old row, a proxy that
      // dropped fields): it must render as placeholders, never as a crash.
      { id: 'co-2' },
    ],
    schedules: [completeSchedule, { id: 'r-2', name: 'تقرير ناقص' }],
  });
  await page.goto('/staff/operations');
  await expect(page.getByRole('heading', { name: 'العمليات والكتالوج' })).toBeVisible();
  await expect(page.getByText('تأمين النيل')).toBeVisible();
  await expect(page.getByText('claims@nile.test')).toBeVisible();
  await expect(page.locator('table.operations-table tbody tr')).toHaveCount(2);

  await page.getByRole('tab', { name: /التقارير المجدولة/ }).click();
  await expect(page.getByText('تقرير أسبوعي')).toBeVisible();
  await expect(page.getByText('ops@example.test')).toBeVisible();
  await expect(page.getByText('تقرير ناقص')).toBeVisible();
  // The schedule without recipients or filters falls back to dashes and 7 days.
  await expect(page.locator('table.operations-table tbody tr')).toHaveCount(2);
  await expect(page.getByText(/7 يوم/).first()).toBeVisible();

  expectCleanBrowser(errors);
});

test('shows the empty state for every tab when the API has no records', async ({ page }) => {
  const errors = await install(page, { companies: [], schedules: [] });
  await page.goto('/staff/operations');
  await expect(page.getByText('لا توجد إعدادات بعد')).toBeVisible();

  for (const tab of [/التقارير المجدولة/, /الفرق/, /قواعد SLA/, /شركات التأمين/]) {
    await page.getByRole('tab', { name: tab }).click();
    await expect(page.getByText('لا توجد إعدادات بعد')).toBeVisible();
  }

  expectCleanBrowser(errors);
});

test('switching tabs quickly never renders a stale response', async ({ page }) => {
  const errors = await install(page, { companies: [completeCompany], schedules: [completeSchedule] });
  // The companies answer is held back until the operator already moved on: the
  // late payload must be dropped instead of being rendered by another table.
  await page.route('**/api/staff/admin/companies', async (route: Route) => {
    await new Promise((resolve) => { setTimeout(resolve, 400); });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([completeCompany]) });
  });
  await page.goto('/staff/operations');
  await page.getByRole('tab', { name: /التقارير المجدولة/ }).click();
  await expect(page.getByText('تقرير أسبوعي')).toBeVisible();
  await page.waitForTimeout(700);
  await expect(page.getByText('ops@example.test')).toBeVisible();
  await expect(page.getByText('تأمين النيل')).toHaveCount(0);

  expectCleanBrowser(errors);
});

test('HTML pattern attributes are regexes a browser can compile', async ({ page }) => {
  const errors = await install(page, { companies: [], schedules: [] });
  await page.goto('/staff/operations');

  // The brand card renders for a platform super admin; its logo path input
  // carried the pattern that threw "Invalid regular expression".
  const logo = page.getByLabel(/رابط\/مسار الشعار/);
  await expect(logo).toBeVisible();
  const logoPattern = await logo.getAttribute('pattern');
  expect(logoPattern).toBeTruthy();
  expect(await page.evaluate((source) => {
    try { return new RegExp(`^(?:${source})$`, 'v').test('/insurance-logo.svg'); } catch { return false; }
  }, logoPattern!)).toBe(true);
  await logo.fill('/assets/brand/logo-2.svg');
  await expect(logo).toHaveJSProperty('validity.valid', true);
  await logo.fill('https://cdn.example.test/logo.svg');
  await expect(logo).toHaveJSProperty('validity.valid', false);

  await page.getByRole('button', { name: /إضافة شركات التأمين/ }).click();
  const slug = page.getByRole('dialog').getByLabel('المعرّف المختصر');
  const slugPattern = await slug.getAttribute('pattern');
  expect(slugPattern).toBeTruthy();
  expect(await page.evaluate((source) => {
    try { return new RegExp(`^(?:${source})$`, 'v').test('nile-assurance'); } catch { return false; }
  }, slugPattern!)).toBe(true);
  await slug.fill('nile-assurance');
  await expect(slug).toHaveJSProperty('validity.valid', true);
  await slug.fill('Nile Assurance');
  await expect(slug).toHaveJSProperty('validity.valid', false);

  expectCleanBrowser(errors);
});
