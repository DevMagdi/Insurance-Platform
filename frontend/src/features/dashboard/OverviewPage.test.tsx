import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { OverviewPage } from './OverviewPage';
import type { StaffAccess } from '../../types';

const apiMock = vi.fn();
vi.mock('../../api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  api: (...args: unknown[]) => apiMock(...args),
}));

const access: StaffAccess = {
  id: 'manager', email: 'manager@example.test', full_name: 'مدير المبيعات', status: 'active',
  token_version: 1, two_factor_enabled: false, two_factor_required: false,
  roles: ['account_manager'], permissions: ['analytics.view', 'complaints.view'], overrides: {},
  all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

function renderDashboard() {
  return render(<OverviewPage
    access={access}
    range={{ from: '2026-10-01', to: '2026-10-26', preset: '30d' }}
    companies={[]}
    onCompany={vi.fn()}
    onInsuranceType={vi.fn()}
    companyId=""
    insuranceType=""
    onOpenComplaints={vi.fn()}
  />);
}

describe('Broker Hub dashboard', () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation(async (url: string) => {
      if (url.includes('/analytics/overview')) return {
        kpis: { total: 12, new: 2, assigned: 3, in_review: 1, resolved: 5, reopened: 1, transferred: 0, sla_breaches: 2, reopen_rate: 0, avg_resolution_hours: 4, csat_avg: 4.5 },
        by_status: [{ status: 'new', count: 2 }, { status: 'assigned', count: 3 }, { status: 'resolved', count: 5 }],
        time_series: [], by_company: [], by_type: [], by_agent: [], resolution_hours: {}, heatmap: [], top_issues: [], company_scorecards: [],
        window: { date_from: '2026-10-01T00:00:00Z', date_to: '2026-10-26T23:59:59Z' },
      };
      if (url.includes('/analytics/channels')) return [{ date: '2026-10-26', whatsapp: 4, calls: 2 }];
      if (url.includes('/complaints')) return { items: [], total: 0, page: 1, page_size: 5 };
      throw new Error(`Unexpected dashboard request: ${url}`);
    });
  });

  it('renders the four broker KPIs, channel chart, status card and latest-requests section from APIs', async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText('إجمالي الطلبات')).toBeTruthy());
    expect(screen.getByText('قيد المعالجة')).toBeTruthy();
    expect(screen.getByText('الالتزام باتفاقية SLA')).toBeTruthy();
    expect(screen.getByText('تمت معالجتها')).toBeTruthy();
    expect(screen.getByText('الطلبات حسب القناة — آخر ٧ أيام')).toBeTruthy();
    expect(screen.getByText('حالة الطلبات')).toBeTruthy();
    expect(screen.getByText('أحدث الطلبات')).toBeTruthy();
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('/api/staff/analytics/channels'));
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('/api/staff/complaints'));
  });

  it('does not present request fixtures when the real list is empty', async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText('لا توجد طلبات حديثة')).toBeTruthy());
    expect(screen.queryByText('REQ-2026-000100')).toBeNull();
  });
});
