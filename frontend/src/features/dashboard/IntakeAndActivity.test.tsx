/**
 * The intake split is the number a shift lead quotes in a stand-up, so it has
 * to be counted the same way the Requests screen counts it: one query per
 * source against the same endpoint, inheriting the dashboard's date filter.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { IntakeBreakdown, RecentActivity } from './IntakeAndActivity';

const apiMock = vi.fn();
vi.mock('../../api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  api: (...args: unknown[]) => apiMock(...args),
}));

describe('IntakeBreakdown', () => {
  beforeEach(() => { apiMock.mockReset(); });

  it('counts whatsapp, phone and email and shows each share', async () => {
    const totals: Record<string, number> = { whatsapp: 60, phone: 30, email: 10 };
    apiMock.mockImplementation(async (url: unknown) => {
      const source = Object.keys(totals).find((key) => String(url).includes(`intake_source=${key}`));
      return { items: [], total: source ? totals[source] : 0, page: 1, page_size: 1 };
    });
    render(<IntakeBreakdown filterQuery="?date_from=2026-10-01" />);
    await waitFor(() => expect(document.querySelectorAll('.ds-intake-row').length).toBe(3));
    const shares = [...document.querySelectorAll('.ds-intake-share')].map((el) => el.textContent);
    expect(shares).toEqual(['٦٠٪', '٣٠٪', '١٠٪']);
  });

  it('carries the dashboard date filter into every source query', async () => {
    apiMock.mockResolvedValue({ items: [], total: 5, page: 1, page_size: 1 });
    render(<IntakeBreakdown filterQuery="?date_from=2026-10-01&date_to=2026-10-21" />);
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(3));
    for (const [url] of apiMock.mock.calls) {
      expect(String(url)).toContain('date_from=2026-10-01');
      expect(String(url)).toContain('date_to=2026-10-21');
    }
  });

  it('does not divide by zero on an empty period', async () => {
    apiMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 1 });
    render(<IntakeBreakdown filterQuery="" />);
    await waitFor(() => expect(document.body.textContent).toContain('لا توجد طلبات في هذه الفترة'));
    expect(document.querySelectorAll('.ds-intake-row').length).toBe(0);
  });

  it('reports a failure instead of showing a silent zero', async () => {
    apiMock.mockImplementation(async () => { throw new Error('boom'); });
    render(<IntakeBreakdown filterQuery="" />);
    await waitFor(() => expect(document.body.textContent).toContain('تعذر حساب المصادر'));
  });
});

describe('RecentActivity', () => {
  beforeEach(() => { apiMock.mockReset(); });

  it('lists the latest requests with status and relative time', async () => {
    apiMock.mockResolvedValue({
      items: [{
        id: 'r1', ticket_no: 'REQ-2026-000100', subject: 'تأخر صرف تعويض', status: 'new',
        channel: 'whatsapp', customer_name: 'منى عبد الله',
        created_at: new Date(Date.now() - 3600_000).toISOString(),
        updated_at: new Date(Date.now() - 1800_000).toISOString(),
      }],
      total: 1, page: 1, page_size: 6,
    });
    render(<RecentActivity />);
    await waitFor(() => expect(document.querySelectorAll('.ds-feed-row').length).toBe(1));
    const row = document.querySelector('.ds-feed-row');
    expect(row?.textContent).toContain('REQ-2026-000100');
    expect(row?.textContent).toContain('منى عبد الله');
    expect(row?.textContent).toContain('منذ ٣٠ دقيقة');
    expect(document.querySelector('.ds-feed-node')?.getAttribute('data-tone')).toBe('new');
  });

  it('falls back to a label when a request has no subject or customer', async () => {
    apiMock.mockResolvedValue({
      items: [{ id: 'r2', ticket_no: 'REQ-2026-000101', subject: '', status: 'resolved',
        channel: 'email', customer_name: '', created_at: new Date().toISOString(),
        updated_at: new Date().toISOString() }],
      total: 1, page: 1, page_size: 6,
    });
    render(<RecentActivity />);
    await waitFor(() => expect(document.querySelectorAll('.ds-feed-row').length).toBe(1));
    const text = document.querySelector('.ds-feed-row')?.textContent ?? '';
    expect(text).toContain('طلب بدون عنوان');
    expect(text).toContain('عميل غير مُعرّف');
  });
});
