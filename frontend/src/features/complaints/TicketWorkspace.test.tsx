import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { ComplaintFilterOptions, StaffAccess } from '../../types';
import { TicketWorkspace } from './TicketWorkspace';

vi.mock('../../api', () => ({
  api: vi.fn(),
  apiResponse: vi.fn(),
  jsonBody: (value: unknown) => JSON.stringify(value),
}));

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();
  constructor(public url: string) { FakeEventSource.instances.push(this); }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  emit(type: string, data: unknown) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    (this.listeners.get(type) ?? []).forEach((listener) => listener(event));
  }
  close() { /* noop */ }
}

afterEach(() => { cleanup(); vi.clearAllMocks(); FakeEventSource.instances = []; });

const access: StaffAccess = {
  id: 'staff-1', email: 'agent@example.test', full_name: 'Agent', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['agent'],
  permissions: ['complaints.view', 'complaints.view_attachments'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const ticket = {
  id: 'ticket-1', ticket_no: 'CMP-2026-000001', status: 'in_review', company_id: 'company-1',
  insurance_type: 'health', subject: 'تأخر التعويض', priority: 'normal', category_id: null,
  channel: 'portal', assigned_agent_id: null, sla_due_at: null, sla_breached_at: null,
  merged_into_id: null, created_at: '2026-10-10T10:00:00Z', updated_at: '2026-10-10T10:00:00Z',
  description: 'الوصف', policy_number: 'POL-1', customer_name: 'منى أحمد', national_id_masked: '**************2345',
};

const options: ComplaintFilterOptions = {
  companies: [{ id: 'company-1', name_en: 'Nile', name_ar: 'النيل', insurance_types: ['health'] }],
  agents: [], statuses: [], priorities: [], channels: [], insurance_types: [],
};

function mockApi(messages: Array<{ id: string; body: string }>) {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === '/api/staff/complaints/ticket-1') return ticket as never;
    if (path === '/api/staff/complaints/ticket-1/timeline') {
      return { events: [], messages: messages.map((m) => ({ ...m, created_at: '2026-10-10T11:00:00Z', visibility: 'public', direction: 'inbound', channel: 'portal' })) } as never;
    }
    if (path === '/api/staff/complaints/ticket-1/attachments') {
      return [{ id: 'att-1', filename: 'report.pdf', mime_type: 'application/pdf', size_bytes: 2048, scan_status: 'clean' }] as never;
    }
    if (path === '/api/staff/complaints/ticket-1/duplicates') return [] as never;
    return [] as never;
  });
}

describe('TicketWorkspace live updates', () => {
  it('shows a clean attachment as openable and refreshes messages on a live update event', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockApi([{ id: 'm1', body: 'الرسالة الأولى' }]);
    render(<TicketWorkspace id="ticket-1" access={access} options={options} onBack={() => undefined} onOpenTicket={() => undefined} onRefreshAccess={async () => undefined} />);

    expect(await screen.findByText('الرسالة الأولى')).toBeTruthy();
    expect(screen.getByText('آمن')).toBeTruthy();
    const attachmentButton = screen.getByRole('button', { name: /report\.pdf/ }) as HTMLButtonElement;
    expect(attachmentButton.disabled).toBe(false);

    // Customer replies; the staff stream emits a new signature and the open
    // ticket must refresh without a manual page reload.
    mockApi([{ id: 'm1', body: 'الرسالة الأولى' }, { id: 'm2', body: 'رد العميل الجديد' }]);
    const source = FakeEventSource.instances[0];
    expect(source.url).toBe('/api/staff/complaints/ticket-1/stream');
    await act(async () => { source.emit('update', { signature: 'sig-2', status: 'in_review' }); });
    expect(await screen.findByText('رد العميل الجديد')).toBeTruthy();
  });
});
