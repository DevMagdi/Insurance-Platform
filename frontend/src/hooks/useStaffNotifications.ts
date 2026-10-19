import { useCallback, useEffect, useState } from 'react';
import { api, jsonBody } from '../api';
import type { NotificationItem, StaffAccess } from '../types';

/**
 * Live staff notifications.
 *
 * The bell must light up while the console is open — a customer complaint or
 * reply fans out to scoped staff on the server (see
 * `app_security.queue_staff_notice`), so the console only has to notice it.
 * Three signals drive a refresh, cheapest first:
 *
 * 1. `staff:data-changed` — dispatched by the live complaints queue (new
 *    ticket over the staff WebSocket) and by an open ticket (customer reply
 *    over SSE), so a new notice appears within a second of the event.
 * 2. `visibilitychange` — the tab becoming visible refreshes immediately.
 * 3. A short interval (10s) while the tab is visible; a hidden tab backs off
 *    completely so a background console costs nothing.
 *
 * Every fetch re-authenticates through the normal session/CSRF path, so a
 * revoked session or a narrowed AccessScope simply stops returning notices.
 */
const POLL_MS = 10_000;

export interface StaffNotificationsState {
  items: NotificationItem[];
  unread: number;
  loading: boolean;
  markRead: (item: NotificationItem) => Promise<void>;
}

export function useStaffNotifications(access: StaffAccess | null): StaffNotificationsState {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (!access) return;
    let active = true;
    const run = async () => {
      if (!active || document.visibilityState !== 'visible') return;
      setPending(true);
      try {
        const result = await api<NotificationItem[]>('/api/me/notifications');
        if (active) setItems(Array.isArray(result) ? result : []);
      } catch {
        // Keep the current list; the next signal retries. A failed poll must
        // never blank the bell.
      } finally {
        if (active) setPending(false);
      }
    };
    void run();
    const timer = window.setInterval(() => { void run(); }, POLL_MS);
    const onVisibility = () => { void run(); };
    const onDataChanged = () => { void run(); };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('staff:data-changed', onDataChanged);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('staff:data-changed', onDataChanged);
    };
  }, [access]);

  const markRead = useCallback(async (item: NotificationItem) => {
    if (item.read_at) return;
    try {
      await api(`/api/me/notifications/${encodeURIComponent(item.id)}/read`, {
        method: 'PATCH',
        body: jsonBody({}),
      });
      setItems((current) =>
        current.map((row) =>
          row.id === item.id ? { ...row, read_at: new Date().toISOString() } : row,
        ),
      );
    } catch {
      // The badge simply stays unread until the next successful poll.
    }
  }, []);

  const unread = items.filter((item) => !item.read_at).length;
  // Only the very first load shows a spinner; live refreshes stay silent.
  return { items, unread, loading: pending && items.length === 0, markRead };
}
