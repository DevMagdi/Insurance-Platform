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
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/me') return json({ kind: 'staff', access });
    if (path === '/api/auth/refresh') return json({ detail: 'expired' }, 401);
    if (path === '/api/auth/staff/login') return json({ next_step: 'totp_verify', challenge: 'challenge' });
    if (path === '/api/staff/complaints/filter-options') {
      return json({ companies: [], statuses: ['new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'], priorities: ['low', 'normal', 'high', 'urgent'], channels: ['portal'], insurance_types: ['health'] });
    }
    if (path === '/api/staff/complaints/counters') return json({ total: 1, open: 1, closed: 0, new: 0, assigned: 0, in_review: 1, resolved: 0, reopened: 0, transferred: 0 });
    if (path === '/api/staff/complaints') return json({ items: [summary], total: 1, page: 1, page_size: 20 });
    if (path === '/api/staff/views' || path === '/api/staff/notifications') return json([]);
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

test('staff queue opens a ticket and generates human-reviewed AI', async ({ page }) => {
  await mockStaffApi(page);
  await page.goto('/staff');
  await expect(page.getByText('CMP-2026-000123')).toBeVisible();
  await page.getByText('CMP-2026-000123').click();
  await expect(page).toHaveURL(new RegExp(`/staff/tickets/${TICKET_ID}`));
  await expect(page.getByText('Delayed claim payment')).toBeVisible();
  await page.getByRole('button', { name: /Generate classification/ }).click();
  await expect(page.getByText('claims')).toBeVisible();
  await expect(page.getByText('mock-llm-v1')).toBeVisible();
});
