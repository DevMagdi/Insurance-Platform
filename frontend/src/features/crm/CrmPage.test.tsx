import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { CrmPage } from './CrmPage';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'staff-1', email: 'sales@example.invalid', full_name: 'Sales', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['account_manager'],
  permissions: ['crm.view', 'crm.manage', 'crm.convert', 'policies.view', 'issuing.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const stages = [
  { code: 'new', name_ar: 'جديد', name_en: 'New', sort_order: 10, is_won: false, is_lost: false, is_converted: false },
  { code: 'won', name_ar: 'ربح', name_en: 'Won', sort_order: 70, is_won: true, is_lost: false, is_converted: false },
];
const lead = {
  id: 'lead-1', lead_no: 'LEAD-2026-ABC123', company_name: 'شركة النور', contact_name: 'سارة',
  mobile: '+201012345678', email: null, source: 'whatsapp', insurance_type: 'health', stage_code: 'new',
  expected_premium: '500000', next_followup_at: null, converted_corporate_client_id: null, activities: [],
};

function mockApi() {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === '/api/staff/crm/stages') return { stages };
    if (path === '/api/staff/crm/pipeline') return { stages, total: 1, won: 0, lost: 0, converted: 0, win_rate: 0, conversion_rate: 0 };
    if (path === '/api/staff/crm/leads') return { leads: [lead] };
    if (path === '/api/staff/crm/leads/lead-1') return lead;
    if (path.startsWith('/api/staff/crm/policies')) return { policies: [] };
    if (path.startsWith('/api/staff/crm/renewals')) return { renewals: [] };
    if (path.startsWith('/api/staff/crm/issuing')) return { issuing: [] };
    if (path.startsWith('/api/staff/crm/insurer-performance')) return { insurers: [] };
    if (path.startsWith('/api/staff/crm/reports')) return { salespeople: [] };
    if (path === '/api/companies') return [];
    if (path === '/api/staff/corporate') return { items: [] };
    return {};
  });
}

describe('CrmPage pipeline', () => {
  it('renders the kanban with leads under their stage and converts on demand', async () => {
    mockApi();
    render(<CrmPage access={access} />);
    expect(await screen.findByText('شركة النور')).toBeTruthy();
    expect(screen.getAllByText('جديد').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText('شركة النور'));
    const convert = await screen.findByText('تحويل إلى عميل فعلي');
    fireEvent.click(convert);
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/api/staff/crm/leads/lead-1/convert', expect.objectContaining({ method: 'POST' })));
  });

  it('denies staff without crm.view', () => {
    render(<CrmPage access={{ ...access, permissions: [] }} />);
    expect(screen.queryByText('إضافة عميل محتمل')).toBeNull();
  });
});
