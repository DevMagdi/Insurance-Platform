import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { CustomersPage } from './CustomersPage';

vi.mock('../../api', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['administrator'], permissions: ['users.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const customer = {
  id: 'customer-1', full_name: 'منى أحمد', email: 'mona@example.test', mobile: '+201123456789',
  whatsapp_available: true, created_at: '2026-10-18T10:00:00Z',
  companies: [{ id: 'company-1', name_en: 'Nile Insurance', name_ar: 'النيل للتأمين' }],
  policies: [{ company_name_en: 'Nile Insurance', company_name_ar: 'النيل للتأمين', insurance_type: 'motor', policy_number: 'POL-321', effective_from: '2026-01-01', expires_at: '2027-01-01' }],
  complaints_count: 1,
  complaints: [{ ticket_no: 'CMP-2026-000001', subject: 'تأخر التعويض', status: 'in_review', created_at: '2026-10-10T10:00:00Z', company_name_en: 'Nile Insurance', company_name_ar: 'النيل للتأمين', insurance_type: 'motor' }],
};

describe('CustomersPage', () => {
  it('filters across client and policy details and opens a complete profile', async () => {
    vi.mocked(api).mockResolvedValue([customer] as never);
    render(<CustomersPage access={access} />);
    expect(await screen.findByText('منى أحمد')).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: 'فلترة العملاء' }), { target: { value: 'POL-321' } });
    expect(screen.getByText('منى أحمد')).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: 'فلترة العملاء' }), { target: { value: 'CMP-2026-000001' } });
    fireEvent.click(screen.getByRole('button', { name: 'منى أحمد' }));
    expect(screen.getByRole('dialog', { name: 'منى أحمد' })).toBeTruthy();
    expect(screen.getByText('ملف العميل الكامل')).toBeTruthy();
    expect(screen.getByText('POL-321')).toBeTruthy();
    expect(screen.getByText(/تأخر التعويض/)).toBeTruthy();
  });
});
