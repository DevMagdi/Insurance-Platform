import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';
import type { NotificationItem, StaffAccess } from '../types';
import { useStaffNotifications } from './useStaffNotifications';

vi.mock('../api', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }));

const access: StaffAccess = {
  id: 'staff-1', email: 'agent@example.test', full_name: 'Agent', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['agent'],
  permissions: ['complaints.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

function notice(id: string, read = false): NotificationItem {
  return {
    id, kind: 'reply', payload: { complaint_id: 'ticket-1', ticket_no: 'CMP-2026-000001' },
    complaint_id: 'ticket-1', created_at: '2026-10-19T10:00:00Z', read_at: read ? '2026-10-19T10:01:00Z' : null,
  };
}

function Harness({ onReady }: { onReady?: (state: ReturnType<typeof useStaffNotifications>) => void }) {
  const state = useStaffNotifications(access);
  onReady?.(state);
  return (
    <output>
      <span data-testid="unread">{state.unread}</span>
      <span data-testid="loading">{state.loading ? 'yes' : 'no'}</span>
      <ul>{state.items.map((item) => <li key={item.id}>{item.id}{item.read_at ? '·read' : ''}</li>)}</ul>
    </output>
  );
}

afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });

describe('useStaffNotifications', () => {
  it('polls while the page is visible and stops while the tab is hidden', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.mocked(api).mockImplementation(async () => [notice('n1')] as never);
    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId('unread').textContent).toBe('1');
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/api/me/notifications')).toHaveLength(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/api/me/notifications')).toHaveLength(2);

    // Hidden tab backs off completely.
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/api/me/notifications')).toHaveLength(2);

    // Becoming visible again refreshes immediately.
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/api/me/notifications')).toHaveLength(3);
  });

  it('refreshes immediately when the live queue or ticket signals a change', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.mocked(api).mockImplementation(async () => [] as never);
    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const polls = () => vi.mocked(api).mock.calls.filter(([path]) => path === '/api/me/notifications').length;
    expect(polls()).toBe(1);

    // A customer complaint arriving over the staff WebSocket (or a reply over
    // the ticket SSE) dispatches this event; the bell must react at once.
    vi.mocked(api).mockImplementation(async () => [notice('n-new')] as never);
    await act(async () => { window.dispatchEvent(new Event('staff:data-changed')); });
    expect(polls()).toBe(2);
    expect(screen.getByTestId('unread').textContent).toBe('1');
    expect(screen.getByText('n-new')).toBeTruthy();
  });

  it('marks a notice read locally and keeps a failed poll silent', async () => {
    vi.mocked(api)
      .mockImplementationOnce(async () => [notice('n1')] as never)
      .mockImplementation(async () => { throw new Error('offline'); });
    let state: ReturnType<typeof useStaffNotifications> | undefined;
    render(<Harness onReady={(value) => { state = value; }} />);
    expect(await screen.findByText('n1')).toBeTruthy();

    vi.mocked(api).mockImplementation(async () => ({ read: true }) as never);
    await act(async () => { await state!.markRead(notice('n1')); });
    expect(vi.mocked(api)).toHaveBeenCalledWith('/api/me/notifications/n1/read', expect.objectContaining({ method: 'PATCH' }));
    expect(screen.getByText('n1·read')).toBeTruthy();
    expect(screen.getByTestId('unread').textContent).toBe('0');

    // A failing refresh keeps the current list instead of blanking the bell.
    vi.mocked(api).mockImplementation(async () => { throw new Error('offline'); });
    await act(async () => { window.dispatchEvent(new Event('staff:data-changed')); });
    expect(screen.getByText('n1·read')).toBeTruthy();
  });

  it('shows the spinner only for the very first load', async () => {
    let resolveFirst: ((value: NotificationItem[]) => void) | undefined;
    vi.mocked(api).mockImplementationOnce(() => new Promise<NotificationItem[]>((resolve) => { resolveFirst = resolve; }));
    render(<Harness />);
    expect(screen.getByTestId('loading').textContent).toBe('yes');
    await act(async () => { resolveFirst?.([notice('n1')]); });
    expect(screen.getByTestId('loading').textContent).toBe('no');
  });
});
