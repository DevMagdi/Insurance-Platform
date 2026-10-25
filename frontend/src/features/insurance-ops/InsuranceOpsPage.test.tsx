import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { InsuranceOpsPage } from './InsuranceOpsPage';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'staff-1', email: 'ops@example.invalid', full_name: 'Ops', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['account_manager'],
  permissions: ['crm.view', 'policies.view', 'issuing.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

function mockApi() {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/crm/leads')) return { items: [] };
    if (path.startsWith('/api/staff/insurance-ops/policies')) return { policies: [{ id: 'p1', corporate_client_id: 'c1', insurer_company_id: 'i1', policy_no: 'POL-100', insurance_type: 'health', starts_on: '2026-01-01', ends_on: '2027-01-01', premium: '120000', installment_plan: 'quarterly', commission_amount: '6000', status: 'active' }] };
    if (path.startsWith('/api/staff/insurance-ops/renewals')) return { renewals: [] };
    if (path.startsWith('/api/staff/insurance-ops/issuing')) return { issuing: [] };
    if (path.startsWith('/api/staff/insurance-ops/imports')) return { imports: [] };
    if (path.startsWith('/api/staff/insurance-ops/insurer-performance')) return { insurers: [] };
    if (path === '/api/companies') return [];
    if (path === '/api/staff/corporate') return { items: [] };
    return {};
  });
}

describe('InsuranceOpsPage', () => {
  it('renders policies with no sales-pipeline tab (CRM lives in /staff/crm-module)', async () => {
    mockApi();
    render(<InsuranceOpsPage access={access} />);
    expect(await screen.findByText('POL-100')).toBeTruthy();
    expect(screen.queryByText('خط المبيعات')).toBeNull();
    expect(screen.queryByText('أداء البياعين')).toBeNull();
    expect(vi.mocked(api)).toHaveBeenCalledWith('/api/staff/insurance-ops/policies');
    expect(vi.mocked(api).mock.calls.some(([path]) => String(path).startsWith('/api/staff/crm'))).toBe(false);
  });

  it('denies staff without crm.view', () => {
    render(<InsuranceOpsPage access={{ ...access, permissions: [] }} />);
    expect(screen.queryByText('POL-100')).toBeNull();
  });
});
