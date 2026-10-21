import { expect, test, type Page, type Route } from '@playwright/test';
import type { Permission, StaffAccess } from '../src/types';

const companyId = 'company-one';
const insideId = '11111111-1111-4111-8111-111111111111';
const outsideId = '22222222-2222-4222-8222-222222222222';
let revoked: boolean;
let exportAttempts: number;
let meRequests: number;

function staffAccess(): StaffAccess {
  const permissions: Permission[] = ['complaints.view', 'analytics.view', 'complaints.export', 'complaints.view_attachments'];
  return {
    id: 'agent-one', email: 'agent@example.invalid', full_name: 'Scoped Agent', status: 'active',
    token_version: revoked ? 2 : 1, two_factor_enabled: true, two_factor_required: true,
    roles: ['Scoped agent'], permissions: revoked ? permissions.filter((permission) => permission !== 'complaints.export') : permissions,
    overrides: {}, all_companies: false, company_ids: [companyId], insurance_types: ['health'], ownership: 'own', team_ids: [],
  };
}
function complaint(id = insideId) {
  return {
    id, ticket_no: 'CMP-2026-000001', status: 'new', company_id: companyId, insurance_type: 'health',
    subject: 'Scoped claim review', priority: 'normal', category_id: null, channel: 'portal',
    assigned_agent_id: 'agent-one', sla_due_at: null, sla_breached_at: null, merged_into_id: null,
    created_at: '2026-10-15T10:00:00Z', updated_at: '2026-10-15T10:00:00Z',
    description: 'Only one-company scope data is visible.', policy_number: 'POLICY-12345',
    customer_name: 'Scoped Customer', national_id_masked: '********1234', company_name: 'One Insurer',
  };
}
function json(value: unknown, status = 200) { return { status, contentType: 'application/json', body: JSON.stringify(value) }; }

async function installScopedApi(page: Page) {
  revoked = false; exportAttempts = 0; meRequests = 0;
  await page.addInitScript(() => {
    document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax';
  });
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/me') {
      meRequests += 1;
      return route.fulfill(json({ kind: 'staff', access: staffAccess() }));
    }
    if (path === '/api/me/notifications') return route.fulfill(json([]));
    if (path === '/api/staff/complaints/filter-options') {
      return route.fulfill(json({
        companies: [{ id: companyId, name_en: 'One Insurer', name_ar: 'شركة واحدة', insurance_types: ['health'] }],
        agents: [{ id: 'agent-one', full_name: 'Scoped Agent' }],
        statuses: ['new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'],
        priorities: ['low', 'normal', 'high', 'urgent'], channels: ['portal'], insurance_types: ['health'],
      }));
    }
    if (path === '/api/staff/analytics/overview') {
      return route.fulfill(json({
        kpis: { total: 1, new: 1, assigned: 0, in_review: 0, resolved: 0, reopened: 0, transferred: 0, avg_resolution_hours: null, sla_breaches: 0, reopen_rate: 0, csat_avg: null },
        time_series: [{ date: '2026-10-15', arrived: 1 }],
        by_status: [{ status: 'new', count: 1 }],
        by_company: [{ company_id: companyId, name: 'One Insurer', count: 1 }],
        by_type: [{ insurance_type: 'health', count: 1 }], by_agent: [{ agent_id: 'agent-one', name: 'Scoped Agent', count: 1 }],
        resolution_hours: { under_24h: 0, '24_to_72h': 0, '72_to_168h': 0, over_168h: 0 },
        heatmap: [], top_issues: [], company_scorecards: [{ company_id: companyId, name: 'One Insurer', total: 1, avg_resolution_hours: null, sla_breaches: 0, csat_avg: null }],
        window: { date_from: '2026-10-01T00:00:00Z', date_to: '2026-10-15T23:59:59Z' },
      }));
    }
    if (path === '/api/staff/complaints/export') {
      exportAttempts += 1;
      if (revoked) return route.fulfill(json({ detail: 'Complaint export permission revoked' }, 403));
      return route.fulfill({ status: 200, contentType: 'text/csv; charset=utf-8', body: 'ticket_no,company\nCMP-2026-000001,One Insurer\n' });
    }
    if (path === '/api/staff/complaints') {
      const search = url.searchParams.get('search') ?? '';
      const requestedCompany = url.searchParams.get('company_id');
      if (requestedCompany && requestedCompany !== companyId) return route.fulfill(json({ detail: 'Outside scope' }, 403));
      const items = search.toLowerCase().includes('outside') ? [] : [complaint()];
      return route.fulfill(json({ items, total: items.length, page: 1, page_size: 20 }));
    }
    if (path === '/api/staff/views') return route.fulfill(json([]));
    if (path === `/api/staff/complaints/${outsideId}`) return route.fulfill(json({ detail: 'Complaint unavailable' }, 404));
    if (path === `/api/staff/complaints/${insideId}`) return route.fulfill(json(complaint()));
    if (path.endsWith('/timeline')) return route.fulfill(json({ events: [], messages: [], page: 1 }));
    if (path.endsWith('/duplicates')) return route.fulfill(json([]));
    return route.fulfill(json({ detail: `Unhandled test API ${path}` }, 404));
  });
}

test.beforeEach(async ({ page }) => installScopedApi(page));

test('one-company scope is reflected in charts, list search, export, and direct ticket URLs', async ({ page }) => {
  await page.goto('/staff/overview');
  await expect(page.getByRole('heading', { name: 'نظرة عامة على الأداء' })).toBeVisible();
  await expect(page.getByText('One Insurer').first()).toBeVisible();
  const companyFilter = page.getByRole('combobox', { name: 'فلتر شركة التأمين' });
  await expect(companyFilter.locator('option')).toHaveCount(2);
  await expect(companyFilter.locator('option').nth(1)).toHaveText('شركة واحدة');
  await expect(page.getByText('Other Insurer')).toHaveCount(0);
  await expect(page.getByText('جميع الشركات')).toHaveCount(0);

  if ((page.viewportSize()?.width ?? 1024) < 768) {
    await page.getByRole('button', { name: 'فتح القائمة' }).click();
  }
  await page.getByRole('navigation', { name: 'مساحة العمل' }).getByRole('button', { name: /قائمة الطلبات/ }).click();
  await expect(page.getByRole('heading', { name: 'صندوق الطلبات' })).toBeVisible();
  await expect(page.getByText('Scoped claim review')).toBeVisible();
  const search = page.getByRole('textbox', { name: 'البحث في الطلبات' });
  await search.fill('OUTSIDE-CLAIM');
  await expect(page.getByText('لا توجد طلبات مطابقة')).toBeVisible();
  await search.fill('');
  await expect(page.getByText('Scoped claim review')).toBeVisible();

  const exportResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/staff/complaints/export');
  await page.getByRole('button', { name: /CSV · Excel-ready/ }).click();
  const exported = await exportResponse;
  expect(exported.status()).toBe(200);
  const csv = await exported.text();
  expect(csv).toContain('One Insurer');
  expect(csv).not.toContain('Other Insurer');
  expect(csv).not.toContain('OUTSIDE-CLAIM');
  expect(exportAttempts).toBe(1);

  const directTicketResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/staff/complaints/${outsideId}`);
  await page.goto(`/staff/complaints/${outsideId}`);
  expect((await directTicketResponse).status()).toBe(404);
  // Out-of-scope tickets stay inside the console shell and show its locked state.
  await expect(page.getByRole('heading', { name: 'الطلب غير متاح' })).toBeVisible();
  await expect(page.getByText('Scoped Customer')).toHaveCount(0);
  await expect(page.getByText('Other Insurer')).toHaveCount(0);
});

test('a live export-permission revocation hides the action and the server rejects the attempted export', async ({ page }) => {
  await page.goto('/staff/complaints');
  const exportButton = page.getByRole('button', { name: /CSV · Excel-ready/ });
  await expect(exportButton).toBeVisible();
  revoked = true;
  const deniedResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/staff/complaints/export');
  await exportButton.click();
  expect((await deniedResponse).status()).toBe(403);
  await expect(page.getByRole('button', { name: /CSV · Excel-ready/ })).toHaveCount(0);
  await expect.poll(() => meRequests).toBeGreaterThan(1);
  expect(exportAttempts).toBe(1);
});
