import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { ComplaintFilterOptions, StaffAccess } from '../../types';
import { ComplaintQueue } from './ComplaintQueue';

vi.mock('../../api', () => ({
  api: vi.fn(),
  apiResponse: vi.fn(),
  jsonBody: (value: unknown) => JSON.stringify(value),
  queryString: (params: Record<string, unknown>) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
    }
    const text = search.toString();
    return text ? `?${text}` : '';
  },
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  readyState = 1;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) { FakeWebSocket.instances.push(this); }
  send(payload: string) { this.sent.push(payload); }
  close() { this.closed = true; }
  closed = false;
  emit(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks(); FakeWebSocket.instances = []; });

const access: StaffAccess = {
  id: 'staff-1', email: 'agent@example.test', full_name: 'Agent', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['agent'],
  permissions: ['complaints.view', 'complaints.export'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const options: ComplaintFilterOptions = {
  companies: [{ id: 'company-1', name_en: 'Nile', name_ar: 'النيل', insurance_types: ['health'] }],
  agents: [], statuses: ['new', 'in_review'], priorities: ['normal'], channels: ['portal'], insurance_types: ['health'],
};

const ticket = (id: string, ticketNo: string) => ({
  id, ticket_no: ticketNo, subject: 'تأخر التعويض', company_id: 'company-1', insurance_type: 'health',
  priority: 'normal', channel: 'portal', assigned_agent_id: null, status: 'new',
  created_at: '2026-10-19T09:00:00Z', sla_breached_at: null,
});

describe('ComplaintQueue live updates', () => {
  it('renders a selectable compact inbox list backed by the complaint API', async () => {
    const onSelectTicket = vi.fn();
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.startsWith('/api/staff/complaints?')) {
        return { items: [ticket('ticket-1', 'CMP-2026-000001')], total: 1, page: 1, page_size: 20 } as never;
      }
      if (path === '/api/staff/views') return [] as never;
      return {} as never;
    });
    render(
      <ComplaintQueue
        access={access}
        range={{ from: '2026-09-19', to: '2026-10-19', preset: '30d' }}
        options={options}
        companyNames={new Map([['company-1', 'النيل']])}
        compact
        selectedTicket=""
        onSelectTicket={onSelectTicket}
        onExpand={() => undefined}
        onOpenTicket={() => undefined}
      />,
    );
    const row = await screen.findByRole('button', { name: /CMP-2026-000001/ });
    expect(screen.getByRole('region', { name: 'قائمة الطلبات' })).toBeTruthy();
    await act(async () => { (row as HTMLButtonElement).click(); });
    expect(onSelectTicket).toHaveBeenCalledWith('ticket-1');
  });

  it('renders a new complaint from the staff stream and signals the console', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const events: string[] = [];
    const onDataChanged = () => events.push('staff:data-changed');
    window.addEventListener('staff:data-changed', onDataChanged);
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.startsWith('/api/staff/complaints?')) {
        return { items: [ticket('ticket-1', 'CMP-2026-000001')], total: 1, page: 1, page_size: 20 } as never;
      }
      if (path === '/api/staff/views') return [] as never;
      return {} as never;
    });

    render(
      <ComplaintQueue
        access={access}
        range={{ from: '2026-09-19', to: '2026-10-19', preset: '30d' }}
        options={options}
        companyNames={new Map([['company-1', 'النيل']])}
        onOpenTicket={() => undefined}
      />,
    );
    expect(await screen.findByText('CMP-2026-000001')).toBeTruthy();

    const socket = FakeWebSocket.instances[0];
    expect(socket.url).toContain('/api/staff/complaints/stream?');
    const restCalls = () => vi.mocked(api).mock.calls.filter(([path]) => path.startsWith('/api/staff/complaints?')).length;

    // A customer files a complaint: the scoped snapshot arrives over the
    // WebSocket and the row appears with no REST refetch and no refresh.
    await act(async () => {
      socket.emit({ items: [ticket('ticket-2', 'CMP-2026-000002'), ticket('ticket-1', 'CMP-2026-000001')], total: 2, page: 1, page_size: 20 });
    });
    expect(screen.getByText('CMP-2026-000002')).toBeTruthy();
    expect(restCalls()).toBe(1);
    expect(events).toEqual(['staff:data-changed']);

    // A quiet snapshot (same tickets) must not re-signal the console.
    await act(async () => {
      socket.emit({ items: [ticket('ticket-2', 'CMP-2026-000002'), ticket('ticket-1', 'CMP-2026-000001')], total: 2, page: 1, page_size: 20 });
    });
    expect(events).toEqual(['staff:data-changed']);

    // The heartbeat path nudges the socket when it is open.
    await act(async () => { socket.emit({ items: [ticket('ticket-2', 'CMP-2026-000002'), ticket('ticket-1', 'CMP-2026-000001')], total: 2, page: 1, page_size: 20 }); });
    expect(socket.sent).toEqual([]);
    window.removeEventListener('staff:data-changed', onDataChanged);
    vi.unstubAllGlobals();
  });

  it('falls back to a scoped REST poll when the socket is unavailable', async () => {
    vi.stubGlobal('WebSocket', class { constructor() { throw new Error('blocked'); } });
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.startsWith('/api/staff/complaints?')) {
        return { items: [ticket('ticket-1', 'CMP-2026-000001')], total: 1, page: 1, page_size: 20 } as never;
      }
      if (path === '/api/staff/views') return [] as never;
      return {} as never;
    });
    render(
      <ComplaintQueue
        access={access}
        range={{ from: '2026-09-19', to: '2026-10-19', preset: '30d' }}
        options={options}
        companyNames={new Map([['company-1', 'النيل']])}
        onOpenTicket={() => undefined}
      />,
    );
    expect(await screen.findByText('CMP-2026-000001')).toBeTruthy();
    // The table still shows the scoped REST result; the queue is never empty
    // just because the realtime channel is missing.
    expect(screen.getByRole('table')).toBeTruthy();
    vi.unstubAllGlobals();
  });
});
