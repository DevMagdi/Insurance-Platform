import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { CrmModulePage } from './CrmModulePage';

vi.mock('../../api', () => ({
  api: vi.fn(),
  apiResponse: vi.fn(),
  jsonBody: (value: unknown) => JSON.stringify(value),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'staff-1', email: 'crm@example.invalid', full_name: 'CRM User', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['sales'],
  permissions: ['crm.view', 'crm.manage'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const lead = { id: 'lead-1', full_name: 'Mona Ali', phone: '+201012345678', email: 'mona@x.com', source: 'import', status: 'new', assigned_to: null, notes: null, created_at: '2026-10-24T09:00:00Z', activities: [{ id: 'a1', type: 'note', description: 'Imported', created_at: '2026-10-24T09:00:00Z' }] };

function mockApi() {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === '/api/crm/stats') return { total_leads: 3, by_status: { new: 2, converted: 1 }, by_source: { import: 3 }, last_30_days: [{ date: '2026-10-24', count: 3 }], conversion_rate: 33.3, recent_leads: [lead], recent_imports: [], recent_activities: [] };
    if (path.startsWith('/api/crm/leads?')) return { items: [lead], total: 1, page: 1, page_size: 20, pages: 1 };
    if (path === '/api/crm/leads/lead-1') return lead;
    if (path === '/api/staff/users') return { items: [{ id: 'staff-1', full_name: 'CRM User' }] };
    if (path === '/api/crm/imports') return { imports: [{ id: 'b1', file_name: 'leads.xlsx', total_rows: 4, success_rows: 2, failed_rows: 2, status: 'completed_with_errors', created_at: '2026-10-24T09:00:00Z' }] };
    return {};
  });
}

describe('CrmModulePage', () => {
  it('renders dashboard stats and recent leads', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="dashboard" onTab={() => undefined} />);
    expect(await screen.findByText('معدل التحويل')).toBeTruthy();
    expect(screen.getByText('33.3٪')).toBeTruthy();
    expect(screen.getByText(/Mona Ali/)).toBeTruthy();
  });

  it('lists leads and opens the detail drawer with activities', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="leads" onTab={() => undefined} />);
    fireEvent.click(await screen.findByText('Mona Ali'));
    expect(await screen.findByText('سجل الأنشطة')).toBeTruthy();
    expect(screen.getByText(/Imported/)).toBeTruthy();
  });

  it('shows import history batches', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="history" onTab={() => undefined} />);
    expect(await screen.findByText('leads.xlsx')).toBeTruthy();
    expect(screen.getByText('completed_with_errors')).toBeTruthy();
  });

  it('denies users without crm.view', async () => {
    render(<CrmModulePage access={{ ...access, permissions: [] }} tab="dashboard" onTab={() => undefined} />);
    await waitFor(() => expect(screen.queryByText('معدل التحويل')).toBeNull());
  });
});
