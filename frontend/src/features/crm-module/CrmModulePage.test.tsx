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
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const access: StaffAccess = {
  id: 'staff-1', email: 'crm@example.invalid', full_name: 'CRM User', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['sales'],
  permissions: ['crm.view', 'crm.manage', 'crm.convert'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const stages = [
  { id: 's1', code: 'new', name_ar: 'جديد', name_en: 'New', sort_order: 10, is_won: false, is_lost: false, is_converted: false, is_active: true, count: 1 },
  { id: 's2', code: 'won', name_ar: 'ربح', name_en: 'Won', sort_order: 70, is_won: true, is_lost: false, is_converted: false, is_active: true, count: 0 },
  { id: 's3', code: 'converted', name_ar: 'محوّل لعميل', name_en: 'Converted', sort_order: 90, is_won: false, is_lost: false, is_converted: true, is_active: true, count: 0 },
];
const lead = {
  id: 'lead-1', lead_no: 'LEAD-2026-ABC123', full_name: 'Mona Ali', company_name: 'شركة النور',
  phone: '+201012345678', email: 'mona@x.com', source: 'whatsapp', stage_code: 'new', priority: 'high',
  insurance_type: 'health', insurer_company_id: null, expected_premium: '500000', assigned_to: 'staff-1',
  notes: null, lost_reason: null, converted_corporate_client_id: null, converted_at: null,
  closed_at: null, next_followup_at: '2026-10-20T09:00:00Z', created_at: '2026-10-24T09:00:00Z',
  activities: [{ id: 'a1', type: 'note', description: 'Imported', task_id: null, created_at: '2026-10-24T09:00:00Z' }],
  tasks: [{ id: 't1', lead_id: 'lead-1', type: 'call', title: 'Chase quote', description: null, due_at: '2026-10-20T09:00:00Z', status: 'pending', is_overdue: true, assigned_to: 'staff-1', completed_at: null }],
};
const perf = [{ staff_id: 'staff-1', full_name: 'CRM User', leads: 4, won: 1, lost: 1, converted: 1, conversion_rate: 25, win_rate: 66.7, won_value: '500000', pipeline_value: '900000', avg_close_days: '12.5', tasks_done: 3, tasks_overdue: 1 }];

function mockApi() {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/crm/stages')) return { stages };
    if (path === '/api/crm/stats') return {
      total_leads: 3,
      by_stage: stages.map((s) => ({ ...s, count: s.code === 'new' ? 3 : 0 })),
      by_source: { whatsapp: 3 },
      last_30_days: [{ date: '2026-10-24', count: 3 }],
      conversion_rate: 33.3,
      due_tasks: { overdue: 2, today: 1, next_7_days: 4 },
      top_performers: perf,
      recent_leads: [lead], recent_imports: [], recent_activities: [],
    };
    if (path.startsWith('/api/crm/pipeline')) return {
      stages: stages.map((s) => ({ ...s, leads: s.code === 'new' ? [lead] : [], value: '500000' })),
      total: 1, won: 0, lost: 0, converted: 0, win_rate: 0, conversion_rate: 0, open_pipeline_value: '500000',
    };
    if (path.startsWith('/api/crm/leads?')) return { items: [lead], total: 1, page: 1, page_size: 20, pages: 1 };
    if (path === '/api/crm/leads/lead-1') return lead;
    if (path.startsWith('/api/crm/tasks')) return { tasks: lead.tasks.map((t) => ({ ...t, lead_name: 'شركة النور', lead_no: lead.lead_no })), due_counts: { overdue: 1, today: 0, next_7_days: 2 } };
    if (path.startsWith('/api/crm/reports/sales-performance')) return { salespeople: perf };
    if (path === '/api/staff/users') return { items: [{ id: 'staff-1', full_name: 'CRM User' }] };
    if (path === '/api/crm/imports') return { imports: [{ id: 'b1', file_name: 'leads.xlsx', total_rows: 4, success_rows: 2, failed_rows: 2, status: 'completed_with_errors', created_at: '2026-10-24T09:00:00Z' }] };
    return {};
  });
}

describe('CrmModulePage (unified CRM)', () => {
  it('renders dashboard stats with pipeline stages, due tasks and top performers', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="dashboard" onTab={() => undefined} />);
    expect(await screen.findByText('معدل التحويل لعميل')).toBeTruthy();
    expect(screen.getByText('33.3٪')).toBeTruthy();
    expect(screen.getByText(/شركة النور/)).toBeTruthy();
    expect(screen.getByText('أفضل البياعين')).toBeTruthy();
  });

  it('renders the kanban and moves a card by drag-and-drop', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="pipeline" onTab={() => undefined} />);
    expect(await screen.findByText('شركة النور')).toBeTruthy();
    const wonColumn = document.querySelector('[data-stage="won"]');
    expect(wonColumn).toBeTruthy();
    fireEvent.drop(wonColumn as Element, {
      dataTransfer: { getData: () => 'lead-1', setData: () => undefined },
    });
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/api/crm/leads/lead-1/move', expect.objectContaining({ method: 'POST' })));
  });

  it('lists leads and opens the drawer with tasks, timeline and convert', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="leads" onTab={() => undefined} />);
    fireEvent.click(await screen.findByText('شركة النور'));
    expect(await screen.findByText('سجل الأنشطة')).toBeTruthy();
    expect(screen.getByText(/Imported/)).toBeTruthy();
    expect(screen.getByText('مهام ومتابعات هذا العميل')).toBeTruthy();
    expect(screen.getByText(/Chase quote/)).toBeTruthy();
    const convert = screen.getByText('تحويل إلى عميل فعلي');
    vi.stubGlobal('confirm', vi.fn(() => true));
    fireEvent.click(convert);
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/api/crm/leads/lead-1/convert', expect.objectContaining({ method: 'POST' })));
  });

  it('shows due task badges on the tasks tab', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="tasks" onTab={() => undefined} />);
    expect(await screen.findByText('Chase quote')).toBeTruthy();
    expect(screen.getAllByText('متأخرة').length).toBeGreaterThan(0);
  });

  it('renders salesperson performance with close-time and deal value', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="performance" onTab={() => undefined} />);
    expect(await screen.findByText(/متوسط زمن الإغلاق/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText('12.5')).toBeTruthy());
  });

  it('denies sales-role access to team performance and stage administration', async () => {
    mockApi();
    const salesperson: StaffAccess = {
      ...access,
      permissions: ['crm.view', 'crm.manage'],
    };
    const { rerender } = render(
      <CrmModulePage access={salesperson} tab="performance" onTab={() => undefined} />,
    );
    expect(await screen.findByText('تقارير فريق المبيعات غير متاحة')).toBeTruthy();
    expect(vi.mocked(api)).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/crm/reports/sales-performance'),
    );

    rerender(<CrmModulePage access={salesperson} tab="pipeline" onTab={() => undefined} />);
    expect(await screen.findByText('شركة النور')).toBeTruthy();
    expect(screen.queryByText('تخصيص المراحل')).toBeNull();
  });

  it('shows import history batches', async () => {
    mockApi();
    render(<CrmModulePage access={access} tab="history" onTab={() => undefined} />);
    expect(await screen.findByText('leads.xlsx')).toBeTruthy();
    expect(screen.getByText('completed_with_errors')).toBeTruthy();
  });

  it('denies users without crm.view', async () => {
    render(<CrmModulePage access={{ ...access, permissions: [] }} tab="dashboard" onTab={() => undefined} />);
    await waitFor(() => expect(screen.queryByText('معدل التحويل لعميل')).toBeNull());
  });
});
