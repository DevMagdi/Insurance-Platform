import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { AnalyticsOverview, StaffAccess } from '../../types';
import { AdminDashboard } from './AdminDashboard';
import { canViewAdminDashboard } from './adminAccess';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: JSON.stringify, queryString: (query: Record<string, string>) => `?date_from=${query.date_from}&date_to=${query.date_to}` }));
const access: StaffAccess = {
  id: 'admin', email: 'admin@example.test', full_name: 'Admin', status: 'active',
  token_version: 0, two_factor_enabled: true, two_factor_required: true,
  roles: [], overrides: {}, team_ids: [],
  all_companies: true, company_ids: [], insurance_types: [], ownership: 'all',
  permissions: ['analytics.view', 'complaints.view', 'users.view', 'audit.view'],
};
const report = {
  kpis: { total: 8, new: 2, reopened: 1, assigned: 3, in_review: 1, resolved: 2, transferred: 0, sla_breaches: 1, avg_resolution_hours: 4, reopen_rate: 0.125, csat_avg: null },
  time_series: [{ date: '2026-10-01', arrived: 3 }, { date: '2026-10-02', arrived: 5 }],
  by_status: [{ status: 'new', count: 2 }], by_company: [{ company_id: 'one', name: 'شركة اختبار', count: 8 }],
} as AnalyticsOverview;
const range = { from: '2026-10-01', to: '2026-10-02', preset: 'custom' } as const;

afterEach(() => vi.clearAllMocks());

describe('first-party admin dashboard', () => {
  it('denies scoped staff and never requests admin data', () => {
    const scoped = { ...access, all_companies: false };
    expect(canViewAdminDashboard(scoped)).toBe(false);
    render(<AdminDashboard access={scoped} range={range} onNavigate={vi.fn()} />);
    expect(screen.getByText('لوحة الإدارة غير متاحة')).toBeTruthy();
    expect(api).not.toHaveBeenCalled();
  });

  it('shows real analytics and permission-filtered navigation', async () => {
    vi.mocked(api).mockImplementation(async (path) => (path === '/api/staff/users' ? [{ id: 'a', status: 'active', two_factor_required: true }] : path === '/api/staff/departments' ? [{ id: 'claims', name: 'التعويضات', description: '', is_active: true, employee_count: 1 }] : report) as never);
    const navigate = vi.fn();
    render(<AdminDashboard access={access} range={range} onNavigate={navigate} />);
    expect(await screen.findByText('شركة اختبار')).toBeTruthy();
    expect(screen.getByText('بانتظار المتابعة')).toBeTruthy();
    expect(screen.getByText('حساب مفعّل')).toBeTruthy();
    expect(screen.queryByText('مركز التخزين')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /سجل التدقيق/ }));
    expect(navigate).toHaveBeenCalledWith({ page: 'audit' });
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => String(path).startsWith('/api/staff/analytics/overview?date_from='))).toBe(true));
  });

  it('opens the full employee profile directly from the dashboard photo', async () => {
    const employee = { id: 'employee', full_name: 'Mona Ali', email: 'mona@company.com', status: 'active', department_id: 'claims', department_name: 'التعويضات', avatar_url: '/api/staff/users/employee/avatar' };
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === '/api/staff/users') return [employee] as never;
      if (path === '/api/staff/departments') return [{ id: 'claims', name: 'التعويضات', description: '', is_active: true, employee_count: 1 }] as never;
      if (path === '/api/staff/users/employee/profile') return { ...employee, personal: { national_id: '29901010112345', mobile: '+201123456789', address: '12 Nile Street', city: 'Giza' } } as never;
      return report as never;
    });
    render(<AdminDashboard access={access} range={range} onNavigate={vi.fn()} />);
    const button = await screen.findByRole('button', { name: 'عرض بروفايل Mona Ali' });
    expect(within(button).getByAltText('صورة Mona Ali').getAttribute('src')).toBe(employee.avatar_url);
    fireEvent.click(button);
    const modal = screen.getByRole('dialog');
    expect(await within(modal).findByText('29901010112345')).toBeTruthy();
    expect(within(modal).getByText('12 Nile Street')).toBeTruthy();
    expect(within(modal).queryByRole('button', { name: 'حفظ القسم' })).toBeNull();
  });

  it('lets a global staff administrator create a department on the dashboard', async () => {
    const catalogue = [{ id: 'claims', name: 'التعويضات', description: '', is_active: true, employee_count: 1 }];
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path === '/api/staff/users') return [] as never;
      if (path === '/api/staff/departments' && init?.method === 'POST') {
        catalogue.push({ id: 'support', ...JSON.parse(init.body as string), employee_count: 0 });
        return catalogue.at(-1) as never;
      }
      if (path === '/api/staff/departments') return [...catalogue] as never;
      return report as never;
    });
    render(<AdminDashboard access={{ ...access, permissions: [...access.permissions, 'users.edit'] }} range={range} onNavigate={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'قسم جديد' }));
    const modal = screen.getByRole('dialog');
    fireEvent.change(within(modal).getByLabelText('اسم القسم'), { target: { value: 'خدمة العملاء' } });
    fireEvent.change(within(modal).getByLabelText('الوصف (اختياري)'), { target: { value: 'متابعة الفريق' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'إنشاء القسم' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, init]) => path === '/api/staff/departments' && init?.method === 'POST' && JSON.parse(init.body as string).description === 'متابعة الفريق')).toBe(true));
    expect(await screen.findByRole('button', { name: 'تعديل قسم خدمة العملاء' })).toBeTruthy();
  });

  it('does not fetch user directory without users.view' , async () => {
    vi.mocked(api).mockResolvedValue(report);
    render(<AdminDashboard access={{ ...access, permissions: ['analytics.view', 'complaints.view'] }} range={range} onNavigate={vi.fn()} />);
    await screen.findByText('شركة اختبار');
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/api/staff/users' || path === '/api/staff/departments')).toBe(false);
    expect(screen.queryByText('إدارة المستخدمين ←')).toBeNull();
  });
});
