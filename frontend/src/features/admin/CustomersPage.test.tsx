import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { CustomersPage } from './CustomersPage';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['administrator'], permissions: ['users.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const company = { id: 'company-1', name_en: 'Nile Insurance', name_ar: 'النيل للتأمين' };
const otherCompany = { id: 'company-2', name_en: 'Delta Insurance', name_ar: 'الدلتا للتأمين' };
const customer = {
  id: 'customer-1', full_name: 'منى أحمد', email: 'mona@example.test', mobile: '+201123456789',
  whatsapp_available: true, created_at: '2026-10-18T10:00:00Z', companies: [company],
  policies: [{ company_name_en: company.name_en, company_name_ar: company.name_ar, insurance_type: 'motor', policy_number: 'POL-321', effective_from: '2026-01-01', expires_at: '2027-01-01' }],
  complaints_count: 1,
  complaints: [{ ticket_no: 'CMP-2026-000001', subject: 'تأخر التعويض', status: 'in_review', created_at: '2026-10-10T10:00:00Z', company_name_en: company.name_en, company_name_ar: company.name_ar, insurance_type: 'motor' }],
};
const secondCustomer = {
  ...customer,
  id: 'customer-2',
  full_name: 'أحمد محمد',
  email: 'ahmed@example.test',
  companies: [otherCompany],
  policies: [{ ...customer.policies[0], company_name_en: otherCompany.name_en, company_name_ar: otherCompany.name_ar, policy_number: 'POL-654' }],
  complaints_count: 0,
  complaints: [],
};
const unassociatedCustomer = {
  ...customer,
  id: 'customer-3',
  full_name: 'عميل غير مرتبط',
  companies: [],
  policies: [],
  complaints_count: 0,
  complaints: [],
};

function mockDirectory(customers: unknown[], companies: unknown[] = [company]) {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === '/api/staff/customers') return customers as never;
    if (path === '/api/companies') return companies as never;
    return [] as never;
  });
}

describe('CustomersPage', () => {
  it('shows the company for a historical complaint even without a policy', async () => {
    mockDirectory([{ ...customer, policies: [] }]);
    render(<CustomersPage access={access} />);
    expect(await screen.findByText('منى أحمد')).toBeTruthy();
    expect(screen.queryByText('غير مرتبطة')).toBeNull();
    expect(screen.getAllByText(company.name_ar).length).toBeGreaterThan(0);
  });
  it('filters across client and policy details and opens a complete profile', async () => {
    mockDirectory([customer]);
    render(<CustomersPage access={access} />);
    expect(await screen.findByText('منى أحمد')).toBeTruthy();
    expect(vi.mocked(api)).toHaveBeenCalledWith('/api/companies');

    fireEvent.change(screen.getByRole('textbox', { name: 'فلترة العملاء' }), { target: { value: 'POL-321' } });
    expect(screen.getByText('منى أحمد')).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: 'فلترة العملاء' }), { target: { value: 'CMP-2026-000001' } });
    fireEvent.click(screen.getByRole('button', { name: 'منى أحمد' }));
    expect(screen.getByRole('dialog', { name: 'منى أحمد' })).toBeTruthy();
    expect(screen.getByText('ملف العميل الكامل')).toBeTruthy();
    expect(screen.getByText('POL-321')).toBeTruthy();
    expect(screen.getByText(/تأخر التعويض/)).toBeTruthy();
  });

  it('loads company filter options even when no listed customer is associated with them', async () => {
    mockDirectory([unassociatedCustomer], [company]);
    render(<CustomersPage access={access} />);

    expect(await screen.findByText('عميل غير مرتبط')).toBeTruthy();
    const companySelect = screen.getByRole('combobox', { name: 'شركة التأمين' }) as HTMLSelectElement;
    expect(companySelect.options).toHaveLength(2);
    expect(companySelect.options[1].value).toBe(company.id);
    expect(companySelect.options[1].textContent).toBe(company.name_ar);

    fireEvent.change(companySelect, { target: { value: company.id } });
    expect(screen.getByText('لا يوجد عملاء مطابقون')).toBeTruthy();
  });

  it('filters by the selected company and restores all clients with the all-companies option', async () => {
    mockDirectory([customer, secondCustomer], [company, otherCompany]);
    render(<CustomersPage access={access} />);

    expect(await screen.findByText('منى أحمد')).toBeTruthy();
    expect(screen.getByText('أحمد محمد')).toBeTruthy();
    const companySelect = screen.getByRole('combobox', { name: 'شركة التأمين' });

    fireEvent.change(companySelect, { target: { value: otherCompany.id } });
    expect(screen.getByText('أحمد محمد')).toBeTruthy();
    expect(screen.queryByText('منى أحمد')).toBeNull();

    fireEvent.change(companySelect, { target: { value: '' } });
    expect(screen.getByText('منى أحمد')).toBeTruthy();
    expect(screen.getByText('أحمد محمد')).toBeTruthy();
  });
});

describe('CustomersPage national ID', () => {
  const privileged: StaffAccess = { ...access, permissions: ['users.view', 'complaints.view_full_national_id'] };

  it('shows masked رقم البطاقة and reveals the full ID with a reason for authorized staff', async () => {
    vi.mocked(api).mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/staff/customers') return [{ ...customer, national_id_last4: '2345' }] as never;
      if (path === '/api/companies') return [company] as never;
      if (path === '/api/staff/customers/customer-1/national-id:reveal' && init?.method === 'POST') {
        return { national_id: '29901010112345' } as never;
      }
      return [] as never;
    });
    render(<CustomersPage access={privileged} />);
    fireEvent.click(await screen.findByRole('button', { name: 'منى أحمد' }));
    expect(screen.getByText('رقم البطاقة')).toBeTruthy();
    expect(screen.getByText('•••• •••• 2345')).toBeTruthy();
    expect(screen.queryByText('29901010112345')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'إظهار رقم البطاقة' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'سبب إظهار رقم البطاقة' }), { target: { value: 'التحقق من هوية العميل' } });
    fireEvent.click(screen.getByRole('button', { name: 'تأكيد الإظهار' }));
    expect(await screen.findByText('29901010112345')).toBeTruthy();
  });

  it('does not offer reveal without the dedicated permission', async () => {
    mockDirectory([{ ...customer, national_id_last4: '2345' }]);
    render(<CustomersPage access={access} />);
    fireEvent.click(await screen.findByRole('button', { name: 'منى أحمد' }));
    expect(screen.getByText('رقم البطاقة')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'إظهار رقم البطاقة' })).toBeNull();
  });
});
