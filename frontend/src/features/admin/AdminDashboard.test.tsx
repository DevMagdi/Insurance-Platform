import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { AnalyticsOverview, StaffAccess } from '../../types';
import { AdminDashboard } from './AdminDashboard';
import { canViewAdminDashboard } from './adminAccess';

vi.mock('../../api', () => ({ api: vi.fn(), queryString: (query: Record<string, string>) => `?date_from=${query.date_from}&date_to=${query.date_to}` }));
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
    vi.mocked(api).mockImplementation(async (path) => (path === '/api/staff/users' ? [{ id: 'a', status: 'active', two_factor_required: true }] : report) as never);
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

  it('does not fetch user directory without users.view', async () => {
    vi.mocked(api).mockResolvedValue(report);
    render(<AdminDashboard access={{ ...access, permissions: ['analytics.view', 'complaints.view'] }} range={range} onNavigate={vi.fn()} />);
    await screen.findByText('شركة اختبار');
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/api/staff/users')).toBe(false);
    expect(screen.queryByText('إدارة المستخدمين ←')).toBeNull();
  });
});
