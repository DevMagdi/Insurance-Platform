import { expect, test, type Page, type Route } from '@playwright/test';

const TICKET_ID = '22222222-2222-4222-8222-222222222222';
const summary = {
  id: TICKET_ID,
  ticket_no: 'CMP-2026-000123',
  status: 'in_review',
  company_id: '11111111-1111-4111-8111-111111111111',
  insurance_type: 'health',
  subject: 'Delayed claim payment',
  priority: 'high',
  category_id: null,
  channel: 'portal',
  assigned_agent_id: null,
  sla_due_at: null,
  response_due_at: null,
  sla_breached_at: null,
  merged_into_id: null,
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-03T10:00:00Z',
};

const access = {
  id: '33333333-3333-4333-8333-333333333333',
  email: 'agent@example.test',
  full_name: 'Karim Adel',
  status: 'active',
  two_factor_enabled: true,
  two_factor_required: false,
  roles: [],
  permissions: [
    'complaints.view',
    'complaints.reply_public',
    'complaints.add_internal_note',
    'complaints.change_status',
    'complaints.view_attachments',
    'complaints.edit',
    'analytics.view',
  ],
  overrides: {},
  all_companies: true,
  company_ids: [],
  insurance_types: [],
  ownership: 'all',
  team_ids: [],
};

async function mockStaffApi(page: Page) {
  let aiGenerated = false;
  await page.addInitScript(() => {
    document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax';
  });
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/me') return json({ kind: 'staff', access });
    if (path === '/api/auth/refresh') return json({ detail: 'expired' }, 401);
    if (path === '/api/auth/staff/login') return json({ next_step: 'totp_verify', challenge: 'challenge' });
    if (path === '/api/staff/analytics/overview') return json({
      kpis: { total: 8, new: 2, reopened: 1, assigned: 3, in_review: 1, resolved: 2, transferred: 0, sla_breaches: 1, avg_resolution_hours: 4, reopen_rate: 0.125, csat_avg: null },
      time_series: [{ date: '2026-10-01', arrived: 3 }, { date: '2026-10-02', arrived: 5 }],
      by_status: [{ status: 'new', count: 2 }], by_company: [{ company_id: summary.company_id, name: 'Nile Insurance', count: 8 }],
      top_issues: [], company_scorecards: [], by_type: [],
    });
    if (path === '/api/staff/complaints/filter-options') {
      return json({ companies: [], agents: [], statuses: ['new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'], priorities: ['low', 'normal', 'high', 'urgent'], channels: ['portal'], insurance_types: ['health'] });
    }
    if (path === '/api/staff/complaints/counters') return json({ total: 1, open: 1, closed: 0, new: 0, assigned: 0, in_review: 1, resolved: 0, reopened: 0, transferred: 0 });
    if (path === '/api/staff/complaints') return json({ items: [summary], total: 1, page: 1, page_size: 20 });
    if (path === '/api/staff/views' || path === '/api/staff/notifications' || path === '/api/me/notifications') return json([]);
    if (path === `/api/staff/complaints/${TICKET_ID}`) {
      return json({ ...summary, description: 'The claim payment is delayed.', policy_number: 'POL-000123', customer_name: 'Mona Ali', national_id_masked: '**********2345', company_name: 'Nile Insurance', assigned_agent_name: null, csat_score: null, can_reopen: false, can_csat: false, resolved_at: null, reopened_count: 0 });
    }
    if (path.endsWith('/timeline')) return json({ events: [], messages: [], page: 1 });
    if (path.endsWith('/attachments') || path.endsWith('/duplicates') || path.endsWith('/canned-replies')) return json([]);
    if (path.endsWith('/ai/analyze')) {
      aiGenerated = true;
      return json({ job_id: 'ai-job-1', state: 'queued', created: true }, 202);
    }
    if (path === '/api/staff/integration-jobs/ai-job-1') {
      return json({ id: 'ai-job-1', kind: 'ai_analyze', state: 'sent', attempt_count: 1, provider_message_id: null, last_error_code: null, created_at: '2026-10-03T10:00:00Z', updated_at: '2026-10-03T10:00:01Z' });
    }
    if (path.endsWith('/ai/insights')) {
      return json(aiGenerated ? [{ id: 'ai-1', kind: 'classification', payload: { category: 'claims' }, confidence: 0.83, provider_model: 'mock-llm-v1', review_status: 'suggested', created_at: '2026-10-03T10:00:00Z' }] : []);
    }
    return json({ detail: 'not mocked' }, 404);
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('insurance-portal:lang', 'en'));
});

test('staff login exposes the TOTP challenge step', async ({ page }) => {
  await mockStaffApi(page);
  await page.goto('/staff/login');
  await page.getByLabel(/Work email/).fill('agent@example.test');
  await page.getByLabel(/Password/).fill('correct-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByLabel(/6-digit authenticator code/)).toBeVisible();
});

test('staff queue opens a ticket and renders the workspace actions', async ({ page }) => {
  await mockStaffApi(page);
  await page.goto('/staff/complaints');
  await expect(page.getByText('CMP-2026-000123')).toBeVisible();
  await page.getByText('CMP-2026-000123').click();
  await expect(page).toHaveURL(new RegExp(`/staff/complaints/${TICKET_ID}`));
  await expect(page.getByText('Delayed claim payment')).toBeVisible();
  await expect(page.getByText('Mona Ali')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'إدارة الحالة' })).toBeVisible();
  await expect(page.getByRole('button', { name: /تحديث الحالة/ })).toBeVisible();
  await expect(page.getByText('رد مقترح بالذكاء الاصطناعي')).toBeVisible();
  await expect(page.getByText('غير مفعّل')).toBeVisible();
});

for (const theme of ['light', 'dark'] as const) {
  test(`premium staff theme remains independent of customer theme: ${theme}`, async ({ page }, testInfo) => {
    await mockStaffApi(page);
    await page.addInitScript((theme) => {
      localStorage.setItem('wasiq-theme', theme);
      localStorage.setItem('insurance-portal:theme', theme === 'dark' ? 'light' : 'dark');
      localStorage.setItem('insurance-portal:lang', 'ar');
    }, theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/staff/complaints');
    await expect(page.getByText('CMP-2026-000123')).toBeVisible();
    await page.getByText('CMP-2026-000123').click();
    await expect(page.getByRole('heading', { name: 'إدارة الحالة' })).toBeVisible();
    const shell = page.locator('.staff-console');
    await expect(shell).toHaveCSS('background-color', theme === 'dark' ? 'rgb(6, 10, 17)' : 'rgb(246, 247, 249)');
    await expect(shell).toHaveCSS('color', theme === 'dark' ? 'rgb(232, 237, 244)' : 'rgb(10, 26, 47)');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`staff-${theme}.png`), fullPage: true });
    await page.goto('/staff/analytics');
    await expect(page.getByText('الشكاوى الواردة يومياً')).toBeVisible();
    await page.goto('/staff/admin');
    await expect(page.locator('.admin-kpi')).toHaveCount(4);
    await expect(page.locator('.admin-trend svg')).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`admin-${theme}.png`), fullPage: true });

  });
}
