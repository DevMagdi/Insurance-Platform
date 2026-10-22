import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { StaffExportsPage } from './StaffAuditExportsPage';

const me = vi.fn();
const exports_ = vi.fn();
const createExport = vi.fn();

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    api: { ...actual.api, me: () => me() },
    staffApi: {
      ...actual.staffApi,
      exports: () => exports_(),
      createExport: (kind: string, filters: unknown) => createExport(kind, filters),
    },
  };
});

const access = {
  id: 'staff-1', email: 'agent@example.test', full_name: 'Mona Ali', status: 'active', token_version: 1,
  two_factor_enabled: false, two_factor_required: false, roles: ['claims'],
  permissions: ['complaints.export', 'analytics.export', 'analytics.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  document.cookie = 'csrf_token=test-token';
  me.mockResolvedValue({ kind: 'staff', access });
  exports_.mockResolvedValue([]);
  createExport.mockResolvedValue({ id: 'job-1' });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('StaffAuditExportsPage', () => {
  it('never sends a broken or oversized date window', async () => {
    render(<StaffExportsPage />, { wrapper: Wrapper });
    const from = await screen.findByLabelText('من');
    const to = screen.getByLabelText('إلى');
    const request = screen.getByRole('button', { name: /تصدير الطلبات/ });

    // Clearing a date must not crash the page and must not send a bad window.
    fireEvent.change(from, { target: { value: '' } });
    await waitFor(() => expect(screen.getByText(/حدّد تاريخ بداية ونهاية صالحين/)).toBeTruthy());
    expect((request as HTMLButtonElement).disabled).toBe(true);
    expect(createExport).not.toHaveBeenCalled();

    // A window longer than 366 days is refused by the API as well.
    fireEvent.change(from, { target: { value: '2024-01-01T00:00' } });
    fireEvent.change(to, { target: { value: '2026-01-01T00:00' } });
    await waitFor(() => expect((request as HTMLButtonElement).disabled).toBe(true));
    expect(createExport).not.toHaveBeenCalled();

    // A valid window is sent.
    fireEvent.change(from, { target: { value: '2026-10-01T00:00' } });
    fireEvent.change(to, { target: { value: '2026-10-15T00:00' } });
    await waitFor(() => expect((request as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(request);
    await waitFor(() => expect(createExport).toHaveBeenCalledOnce());
    const [kind, filters] = createExport.mock.calls[0];
    expect(kind).toBe('complaints');
    expect((filters as { date_from: string }).date_from).toBe(new Date('2026-10-01T00:00').toISOString());
  });
});
