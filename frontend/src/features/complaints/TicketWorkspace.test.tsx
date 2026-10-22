import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, apiResponse } from '../../api';
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
    // The open ticket also tells the console that staff data changed, so the
    // notification badge reflects the same event without a page refresh.
    const signals: string[] = [];
    const onDataChanged = () => signals.push('staff:data-changed');
    window.addEventListener('staff:data-changed', onDataChanged);
    await act(async () => { source.emit('update', { signature: 'sig-2', status: 'in_review' }); });
    expect(screen.getByText('رد العميل الجديد')).toBeTruthy();
    expect(signals).toEqual(['staff:data-changed']);
    window.removeEventListener('staff:data-changed', onDataChanged);
  });
});

describe('TicketWorkspace call recordings', () => {
  function mockApiWithCall(call: Record<string, unknown>) {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/complaints/ticket-1') return ticket as never;
      if (path === '/api/staff/complaints/ticket-1/timeline') return { events: [], messages: [] } as never;
      if (path === '/api/staff/complaints/ticket-1/inbox') {
        return { ticket_no: 'CMP-2026-000001', items: [{ kind: 'call', ...call }] } as never;
      }
      return [] as never;
    });
  }

  const recordedCall = {
    id: 'call-1', direction: 'inbound', status: 'completed', outcome: 'completed',
    transcript: 'العميل بيسأل عن تأخر التعويض', consent_at: '2026-10-22T16:30:35Z',
    started_at: '2026-10-22T16:30:00Z', ended_at: '2026-10-22T16:30:31Z',
    has_recording: true, duration_seconds: 31,
  };

  it('plays a recorded call from the conversation instead of hiding it behind a URL', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockApiWithCall(recordedCall);
    const created: string[] = [];
    // Replace only the two object-URL helpers: stubbing the whole URL global
    // takes the constructor with it, and jsdom needs that for cookies.
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = ((blob: Blob) => { created.push(blob.type); return 'blob:recording-1'; }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => undefined) as typeof URL.revokeObjectURL;
    vi.mocked(apiResponse).mockResolvedValue({
      blob: async () => new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/mpeg' }),
    } as never);

    render(<TicketWorkspace id="ticket-1" access={access} options={options} onBack={() => undefined} onOpenTicket={() => undefined} onRefreshAccess={async () => undefined} />);

    expect(await screen.findByText('مكالمة واردة')).toBeTruthy();
    expect(screen.getByText('31 ثانية')).toBeTruthy();
    expect(screen.getByText('العميل بيسأل عن تأخر التعويض')).toBeTruthy();

    const play = screen.getByRole('button', { name: /تشغيل التسجيل/ });
    await act(async () => { play.click(); });

    // The audited route is the only way to the bytes, and the player gets a
    // typed blob so the browser will actually render controls for it.
    expect(vi.mocked(apiResponse).mock.calls[0][0]).toBe('/api/staff/complaints/ticket-1/calls/call-1/recording');
    expect(created).toEqual(['audio/mpeg']);
    expect(document.querySelector('audio')?.getAttribute('src')).toBe('blob:recording-1');

    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  });

  it('explains a call with no recording instead of offering a dead button', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockApiWithCall({ ...recordedCall, has_recording: false, consent_at: null, transcript: null });

    render(<TicketWorkspace id="ticket-1" access={access} options={options} onBack={() => undefined} onOpenTicket={() => undefined} onRefreshAccess={async () => undefined} />);

    expect(await screen.findByText('المتصل لم يوافق على التسجيل.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /تشغيل التسجيل/ })).toBeNull();
  });
});
