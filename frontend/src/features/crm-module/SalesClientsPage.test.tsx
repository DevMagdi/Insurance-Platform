import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { SalesClientsPage } from './SalesClientsPage';

vi.mock('../../api', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); window.localStorage.clear(); });

const seller: StaffAccess = {
  id: 'seller-a', email: 'seller@example.invalid', full_name: 'Seller A', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: false, roles: ['sales'],
  permissions: ['crm.view', 'crm.manage'], overrides: {}, all_companies: false,
  company_ids: [], insurance_types: [], ownership: 'own', team_ids: [],
};
const ownClient = {
  id: 'client-a', name_ar: 'شركة النور', name_en: 'Al Noor', contact_name: 'Mona Ali',
  contact_mobile: '+201012345678', converted_at: new Date(Date.now() - 60_000).toISOString(), lead_no: 'LEAD-A',
  origin: 'converted_lead',
};

describe('SalesClientsPage', () => {
  it('loads only the server-scoped converted client list and opens a client folder', async () => {
    vi.mocked(api).mockResolvedValue({ items: [ownClient], total: 1, scope: 'assigned-converted-leads' });
    const onOpenFolder = vi.fn();
    render(<SalesClientsPage access={seller} onOpenFolder={onOpenFolder} />);

    expect(await screen.findByText('شركة النور')).toBeTruthy();
    expect(screen.getByText('LEAD-A')).toBeTruthy();
    expect(vi.mocked(api)).toHaveBeenCalledWith('/api/staff/corporate/sales-directory');
    screen.getAllByRole('button', { name: 'فتح الملف' }).forEach((button) => button.click());
    expect(onOpenFolder).toHaveBeenCalledWith('client-a');
  });

  it('shows the conversion banner only after the converted client is returned by the server', async () => {
    window.localStorage.setItem('brokerhub:last-converted-client', JSON.stringify({ id: 'client-a', name: 'شركة النور', at: Date.now() }));
    vi.mocked(api).mockResolvedValue({ items: [ownClient], total: 1, scope: 'assigned-converted-leads' });
    render(<SalesClientsPage access={seller} onOpenFolder={() => undefined} />);

    expect(await screen.findByText('تم التحويل بنجاح')).toBeTruthy();
    expect(screen.getByText(/أصبح في قائمة عملائك/)).toBeTruthy();
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('فتح ملف العميل'));
  });

  it('shows the latest recent server-side conversion to another salesperson without local browser state', async () => {
    vi.mocked(api).mockResolvedValue({ items: [ownClient], total: 1, scope: 'assigned-converted-leads' });
    render(<SalesClientsPage access={seller} onOpenFolder={() => undefined} />);

    expect(await screen.findByText('تم التحويل بنجاح')).toBeTruthy();
    expect((await screen.findAllByText(/شركة النور/)).length).toBeGreaterThan(0);
  });

  it('keeps the list empty and does not invent a conversion banner when the server has no clients', async () => {
    window.localStorage.setItem('brokerhub:last-converted-client', JSON.stringify({ id: 'client-b', name: 'شركة غير مرتبطة', at: Date.now() }));
    vi.mocked(api).mockResolvedValue({ items: [], total: 0, scope: 'assigned-converted-leads' });
    render(<SalesClientsPage access={seller} onOpenFolder={() => undefined} />);

    expect(await screen.findByText('لا يوجد عملاء محوّلون بعد')).toBeTruthy();
    expect(screen.queryByText('تم التحويل بنجاح')).toBeNull();
  });
});
