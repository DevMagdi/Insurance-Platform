import { expect, test, type Page, type Route } from '@playwright/test';

const COMPANY = {
  id: '11111111-1111-4111-8111-111111111111',
  name_en: 'Nile Insurance',
  name_ar: 'النيل للتأمين',
  insurance_types: ['health', 'general', 'both'],
};

const TICKET_ID = '22222222-2222-4222-8222-222222222222';

const summary = {
  id: TICKET_ID,
  ticket_no: 'CMP-2026-000123',
  status: 'in_review',
  company_id: COMPANY.id,
  insurance_type: 'health',
  subject: 'Delayed claim payment',
  priority: 'normal',
  category_id: null,
  channel: 'portal',
  assigned_agent_id: '33333333-3333-4333-8333-333333333333',
  sla_due_at: null,
  response_due_at: null,
  sla_breached_at: null,
  merged_into_id: null,
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-03T10:00:00Z',
};

const consents: unknown[] = [];

async function mockApi(page: Page, { authed = false } = {}) {
  if (authed) {
    await page.addInitScript(() => {
      document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax';
    });
  }
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (path === '/api/me') {
      return authed
        ? json({ kind: 'customer', id: 'cust-1', full_name: 'Mona Ali', mobile: '+201123456789', email: null, national_id_masked: '**********2345' })
        : json({ detail: 'unauthorized' }, 401);
    }
    if (path === '/api/auth/refresh') return json({ detail: 'expired' }, 401);
    if (path === '/api/companies') return json([COMPANY]);
    if (path === '/api/guest/track') {
      return json({
        public_ticket: 'CMP-2026-000123',
        public_status: 'in_review',
        public_company: 'Nile Insurance',
        created: '2026-10-01T10:00:00Z',
      });
    }
    if (path === '/api/public/config') return json({ captcha: null, privacy_policy_version: '2026-10' });
    if (path === '/api/customer/privacy/consents' && route.request().method() === 'POST') {
      consents.push(route.request().postDataJSON());
      return json({ purpose: 'complaint_processing', granted: true }, 201);
    }
    if (path === '/api/auth/customer/otp/start') return json({ message: 'sent' }, 202);
    if (path === '/api/auth/customer/otp/verify') return json({ authenticated_as: 'customer' });
    if (path === '/api/customer/complaints' && route.request().method() === 'POST') {
      return json({ ...summary, status: 'new', description: 'Details', policy_number: 'POL-000123', customer_name: 'Mona Ali', national_id_masked: '**********2345', company_name: 'Nile Insurance', assigned_agent_name: null, csat_score: null, can_reopen: false, can_csat: false, resolved_at: null, reopened_count: 0, replayed: false }, 201);
    }
    if (path === '/api/customer/complaints/counters') {
      return json({ total: 3, open: 2, closed: 1, new: 1, assigned: 0, in_review: 1, resolved: 1, reopened: 0, transferred: 1 });
    }
    if (path === '/api/customer/complaints') return json({ items: [summary], total: 1, page: 1, page_size: 10 });
    if (path === `/api/customer/complaints/${TICKET_ID}`) {
      return json({ ...summary, description: 'The payment has not arrived after the expected date.', policy_number: 'POL-000123', customer_name: 'Mona Ali', national_id_masked: '**********2345', company_name: 'Nile Insurance', assigned_agent_name: 'Karim Adel', csat_score: null, can_reopen: false, can_csat: false, resolved_at: null, reopened_count: 0 });
    }
    if (path.endsWith('/timeline')) {
      return json({
        events: [
          { id: 'e1', event_type: 'created', from_status: null, to_status: 'new', occurred_at: '2026-10-01T10:00:00Z' },
          { id: 'e2', event_type: 'status_changed', from_status: 'new', to_status: 'assigned', occurred_at: '2026-10-02T10:00:00Z' },
          { id: 'e3', event_type: 'status_changed', from_status: 'assigned', to_status: 'in_review', occurred_at: '2026-10-03T10:00:00Z' },
        ],
        messages: [{ id: 'm1', body: 'We are reviewing your documents.', visibility: 'public', direction: 'outbound', created_at: '2026-10-03T11:00:00Z' }],
        page: 1,
      });
    }
    if (path.endsWith('/attachments')) return json([]);
    if (path.endsWith('/stream')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: snapshot\ndata: {"status":"in_review","ticket_no":"CMP-2026-000123"}\n\n',
      });
    }
    if (path === '/api/customer/profile') {
      return json({ id: 'cust-1', full_name: 'Mona Ali', mobile: '+201123456789', email: 'mona@example.com', whatsapp_available: true, national_id_masked: '**********2345', notification_preferences: { email: true, whatsapp: true, sms: false } });
    }
    if (path === '/api/customer/notifications') return json([]);
    return json({ detail: 'not mocked' }, 404);
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('insurance-portal:lang', 'en'));
});

test('landing offers filing and tracking, with language switch', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /complaint deserves VIP/ })).toBeVisible();
  await page.getByRole('button', { name: 'العربية' }).click();
  await expect(page.getByRole('heading', { name: /شكواك تستحق/ })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
});

test('guest tracking returns public status for ticket + national ID', async ({ page }) => {
  await mockApi(page);
  await page.goto('/track');
  await page.getByLabel(/Ticket number/).fill('CMP-2026-000123');
  await page.getByLabel(/National ID/).fill('29901010112345');
  await page.getByRole('button', { name: 'Track complaint' }).click();
  await expect(page.getByText('CMP-2026-000123').first()).toBeVisible();
  await expect(page.getByText('In Review').first()).toBeVisible();
});

test('guest tracking validates the national ID before submitting', async ({ page }) => {
  await mockApi(page);
  await page.goto('/track');
  await page.getByLabel(/Ticket number/).fill('CMP-2026-000123');
  await page.getByLabel(/National ID/).fill('123');
  await page.getByRole('button', { name: 'Track complaint' }).click();
  await expect(page.getByText(/Invalid national ID/)).toBeVisible();
});

test('multi-step complaint wizard reaches the success ticket number', async ({ page }) => {
  await mockApi(page);
  await page.goto('/complaints/new');

  await page.getByLabel(/Full name/).fill('Mona Ali');
  await page.getByLabel(/National ID/).fill('29901010112345');
  await page.getByLabel(/Mobile number/).fill('01123456789');
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByLabel(/Insurance company/).selectOption(COMPANY.id);
  await page.getByRole('radio', { name: 'Health', exact: true }).click();
  await page.getByLabel(/Policy number/).fill('POL-000123');
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByLabel(/Complaint subject/).fill('Delayed claim payment');
  await page.getByLabel(/Complaint details/).fill('The payment has not arrived after the expected date and nobody replies.');
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByRole('button', { name: 'Next' }).click(); // attachments (optional) → review

  await expect(page.getByText('Review before submitting')).toBeVisible();
  await expect(page.getByText('•••• •••• ••23 45')).toBeVisible();

  await page.getByRole('button', { name: 'Send verification code' }).click();
  await page.getByLabel(/Verification code/).fill('123456');
  await page.getByRole('button', { name: 'Submit complaint now' }).click();
  await expect(page.getByText(/Consent to data processing is required/)).toBeVisible();
  await page.getByTestId('privacy-consent').check();
  await page.getByRole('button', { name: 'Submit complaint now' }).click();

  await expect(page.getByTestId('ticket-number')).toHaveText('CMP-2026-000123');
  expect(consents).toContainEqual({ purpose: 'complaint_processing', granted: true });
});

test('my tickets dashboard shows counters and opens the animated timeline', async ({ page }) => {
  await mockApi(page, { authed: true });
  await page.goto('/tickets');
  await expect(page.getByTestId('counter-total')).toHaveText('3');
  await expect(page.getByTestId('counter-transferred')).toHaveText('1');
  await page.getByText('Delayed claim payment').click();
  await expect(page).toHaveURL(new RegExp(`/tickets/${TICKET_ID}`));
  // The agent name appears in the timeline and in the overview side list.
  await expect(page.getByText('Karim Adel').first()).toBeVisible();
  await expect(page.getByText('We are reviewing your documents.')).toBeVisible();
});
