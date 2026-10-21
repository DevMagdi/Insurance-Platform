/**
 * Corporate portal tests.
 *
 * The fixtures below are copies of real `/api/corporate/*` responses, so a
 * change to the server payload shape fails here rather than in the browser.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import '../../i18n';
import { CorporatePortal } from './CorporatePortal';
import { CorporateLoginPage } from './CorporateLoginPage';
import { formatMoney, renewalNotice } from '../../lib/corporate';

const OVERVIEW = {
  client: {
    id: 'f787c4ff-9ac6-46b0-910d-6113477d4c48',
    slug: 'acme-eg',
    address: null,
    name_ar: 'شركة أكمي',
    name_en: 'Acme Egypt',
    contact_name: null,
    contact_email: null,
    contact_mobile: '+201012345678',
  },
  policies: [],
  contracts: [
    {
      id: '0515b16c-7979-4418-814d-9887890853ca',
      status: 'active',
      currency: 'EGP',
      documents: [],
      headcount: 430,
      renews_on: '2027-01-01',
      starts_on: '2026-01-01',
      contract_no: 'CT-ACME-1',
      contract_value: 2500000.0,
      days_to_renewal: 72,
      payment_frequency: 'quarterly',
    },
  ],
  requests_open: 2,
  requests_total: 2,
  employees_count: 4,
};

const MEMBERS = {
  items: [
    {
      id: '32d6c72b-6a44-4681-8435-c1697ce6c638',
      email: null,
      mobile: '+201500000002',
      full_name: 'Karim Nabil',
      employee_code: 'E-0002',
      requests_count: 1,
      requests: [
        {
          id: '2265d3c6-4397-4a97-aeb3-0c412e5347e1',
          status: 'new',
          channel: 'whatsapp',
          subject: 'طلب موافقة',
          ticket_no: 'REQ-2026-000054',
          created_at: '2026-10-21T15:11:21.057078+00:00',
          updated_at: '2026-10-21T15:11:21.057078+00:00',
          card_number: 'CARD-0002',
          intake_source: 'whatsapp',
          policy_number: null,
        },
      ],
    },
    {
      id: 'e5afcb21-4b31-4106-8c0e-849bfa72af58',
      email: null,
      mobile: '+201500000001',
      full_name: 'سعاد فؤاد',
      employee_code: 'E-0001',
      requests_count: 0,
      requests: [],
    },
  ],
  total: 2,
};

/** Records the URLs the portal asks for, so searches can be asserted. */
function mockFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/corporate/overview')) {
      return Response.json(overrides.overview ?? OVERVIEW);
    }
    if (url.includes('/api/corporate/members')) {
      const search = new URL(url, 'https://test').searchParams.get('search');
      if (!search) return Response.json(MEMBERS);
      const items = MEMBERS.items.filter((m) => m.full_name.includes(search));
      return Response.json({ items, total: items.length });
    }
    if (url.includes('/api/corporate/documents')) return Response.json({ items: [], total: 0 });
    return new Response('{}', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

describe('corporate portal', () => {
  it('shows contract value, payment frequency and the renewal countdown', async () => {
    mockFetch();
    render(<CorporatePortal />);

    await waitFor(() => expect(screen.getByRole('heading', { name: 'شركة أكمي' })).toBeTruthy());
    expect(screen.getByText('CT-ACME-1')).toBeTruthy();
    // The three facts the client specifically asked to see.
    expect(screen.getByText(/٢٬٥٠٠٬٠٠٠/)).toBeTruthy();
    expect(screen.getByText('ربع سنوي')).toBeTruthy();
    expect(screen.getByText('يُجدَّد خلال 72 يوم')).toBeTruthy();
    expect(screen.getByText('ساري')).toBeTruthy();
  });

  it('renders the request and employee counters', async () => {
    mockFetch();
    render(<CorporatePortal />);
    await waitFor(() => expect(screen.getByText('عدد الموظفين')).toBeTruthy());
    expect(screen.getByText('إجمالي الطلبات')).toBeTruthy();
    expect(screen.getByText('الطلبات المفتوحة')).toBeTruthy();
  });

  it('lists employees and filters them by name', async () => {
    const calls = mockFetch();
    render(<CorporatePortal />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'شركة أكمي' })).toBeTruthy());

    fireEvent.click(screen.getByText('طلبات الموظفين'));
    await waitFor(() => expect(screen.getByText('Karim Nabil')).toBeTruthy());
    expect(screen.getByText('سعاد فؤاد')).toBeTruthy();

    fireEvent.change(screen.getByLabelText('بحث باسم الموظف'), { target: { value: 'سعاد' } });
    await waitFor(() => expect(screen.queryByText('Karim Nabil')).toBeNull(), { timeout: 2000 });
    expect(screen.getByText('سعاد فؤاد')).toBeTruthy();
    expect(calls.some((url) => url.includes('search=') && url.includes('/members'))).toBe(true);
  });

  it("expands an employee to reveal that employee's requests", async () => {
    mockFetch();
    render(<CorporatePortal />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'شركة أكمي' })).toBeTruthy());
    fireEvent.click(screen.getByText('طلبات الموظفين'));
    await waitFor(() => expect(screen.getByText('Karim Nabil')).toBeTruthy());

    expect(screen.queryByText('REQ-2026-000054')).toBeNull();
    fireEvent.click(screen.getByText('1 طلب'));
    await waitFor(() => expect(screen.getByText('REQ-2026-000054')).toBeTruthy());
    // The request is labelled by its intake channel and its card number.
    expect(screen.getByText('واتساب')).toBeTruthy();
    expect(screen.getByText('CARD-0002')).toBeTruthy();
    expect(screen.getByText('جديد')).toBeTruthy();
  });

  it('tells the user when a name matches nobody', async () => {
    mockFetch();
    render(<CorporatePortal />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'شركة أكمي' })).toBeTruthy());
    fireEvent.click(screen.getByText('طلبات الموظفين'));
    await waitFor(() => expect(screen.getByText('Karim Nabil')).toBeTruthy());

    fireEvent.change(screen.getByLabelText('بحث باسم الموظف'), { target: { value: 'zzz' } });
    await waitFor(() => expect(screen.getByText(/لا يوجد موظف مطابق/)).toBeTruthy(), { timeout: 2000 });
  });

  it('redirects an unauthenticated visitor to the sign-in page', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"Invalid or expired token"}', { status: 401 })));
    const replaceState = vi.spyOn(window.history, 'pushState');
    render(<CorporatePortal />);
    await waitFor(() => expect(replaceState).toHaveBeenCalled());
    expect(String(replaceState.mock.calls[0]?.[2] ?? '')).toContain('/corporate/login');
    replaceState.mockRestore();
  });
});

describe('corporate sign-in', () => {
  it('reports wrong credentials in Arabic instead of a status code', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response('{"detail":"Invalid credentials"}', { status: 401 })));
    render(<CorporateLoginPage />);

    fireEvent.change(screen.getByPlaceholderText('company.hr'), { target: { value: 'acme.hr' } });
    const password = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(password, { target: { value: 'wrong-password' } });
    fireEvent.click(screen.getByText('تسجيل الدخول'));

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('غير صحيحة'));
  });

  it('asks for a new password when the account is still on its first one', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      Response.json({ next_step: 'change_password', full_name: 'HR Acme', must_change_password: true })));
    render(<CorporateLoginPage />);

    fireEvent.change(screen.getByPlaceholderText('company.hr'), { target: { value: 'acme.hr' } });
    const password = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(password, { target: { value: 'first-password' } });
    fireEvent.click(screen.getByText('تسجيل الدخول'));

    await waitFor(() => expect(screen.getByText('كلمة المرور الجديدة')).toBeTruthy());
    expect(screen.getByRole('status').textContent).toContain('تغيير كلمة المرور');
  });
});

describe('corporate formatting', () => {
  it('formats contract money with its currency in the app-wide ar-EG style', () => {
    // The platform formats every number with `ar-EG` (see src/utils/date.ts),
    // which uses Arabic-Indic digits; the portal must not diverge from that.
    expect(formatMoney(2500000, 'EGP')).toBe('٢٬٥٠٠٬٠٠٠ EGP');
  });

  it('words the renewal countdown by urgency, including the past', () => {
    expect(renewalNotice(72)).toEqual({ text: 'يُجدَّد خلال 72 يوم', tone: 'calm' });
    expect(renewalNotice(12).tone).toBe('warn');
    expect(renewalNotice(1)).toEqual({ text: 'يُجدَّد غداً', tone: 'alert' });
    expect(renewalNotice(0)).toEqual({ text: 'يُجدَّد اليوم', tone: 'alert' });
    expect(renewalNotice(-3)).toEqual({ text: 'انتهى منذ 3 يوم', tone: 'alert' });
    expect(renewalNotice(null)).toEqual({ text: 'غير محدد', tone: 'calm' });
  });
});
