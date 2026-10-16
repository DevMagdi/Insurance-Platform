import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CompanyOption, StaffAccess } from '../../types';
import { OperationsPage } from './OperationsPage';
import { api } from '../../api';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['Admin'], permissions: ['companies.manage'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};
const scopedCompanies: CompanyOption[] = [{ id: 'co-1', name_en: 'Scoped Insurer', name_ar: 'شركة ضمن النطاق', insurance_types: ['health'] }];

describe('OperationsPage permission boundaries', () => {
  it('shows only permitted admin sections and records', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/admin/companies') return [{
        id: 'co-1', slug: 'scoped-insurer', name_en: 'Scoped Insurer', name_ar: 'شركة ضمن النطاق',
        contact_emails: [], supported_types: ['health'], is_active: true,
      }] as never;
      return [] as never;
    });
    render(<OperationsPage access={access} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByText('شركة ضمن النطاق')).toBeTruthy();
    expect(screen.queryByRole('tab', { name: /الفرق/ })).toBeNull();
    expect(screen.queryByRole('tab', { name: /التكاملات/ })).toBeNull();
    expect(screen.getByRole('button', { name: /إضافة شركات التأمين/ })).toBeTruthy();
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/api/staff/admin/automation-options')).toBe(false);
  });

  it('removes the operations console when effective permission is revoked', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    const { rerender } = render(<OperationsPage access={access} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /شركات التأمين/ })).toBeTruthy();
    rerender(<OperationsPage access={{ ...access, permissions: [] }} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.queryByRole('tab', { name: /شركات التأمين/ })).toBeNull();
    await waitFor(() => expect(screen.getByText('إعدادات الإدارة غير متاحة')).toBeTruthy());
  });
});
