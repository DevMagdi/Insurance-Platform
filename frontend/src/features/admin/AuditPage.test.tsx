import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { DateRange, StaffAccess } from '../../types';
import { AuditPage } from './AuditPage';

vi.mock('../../api', () => ({ api: vi.fn(), apiResponse: vi.fn(), queryString: (values: Record<string, string | number | undefined | null>) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== '') params.set(key, String(value)); });
  return params.size ? `?${params.toString()}` : '';
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const range: DateRange = { from: '2026-10-01', to: '2026-10-15', preset: 'custom' };
const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['administrator'], permissions: ['audit.view', 'users.manage_access'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

describe('AuditPage filters and access boundaries', () => {
  it('sends event and access-change filters to their permission-protected APIs', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<AuditPage access={access} range={range} />);
    expect(await screen.findByRole('tab', { name: /الأحداث/ })).toBeTruthy();
    fireEvent.change(screen.getByRole('combobox', { name: 'تصفية حسب الإجراء' }), { target: { value: 'status_changed' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'تصفية حسب معرّف الموظف' }), { target: { value: '11111111-1111-4111-8111-111111111111' } });
    fireEvent.click(screen.getByRole('button', { name: /تطبيق الفلاتر/ }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => typeof path === 'string' && path.includes('action=status_changed') && path.includes('actor_id=11111111'))).toBe(true));

    fireEvent.click(screen.getByRole('tab', { name: /تغييرات الوصول/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'تصفية حسب المستخدم المستهدف' }), { target: { value: '22222222-2222-4222-8222-222222222222' } });
    fireEvent.click(screen.getByRole('button', { name: /تطبيق الفلاتر/ }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => typeof path === 'string' && path.includes('/access-changes?') && path.includes('target_id=22222222'))).toBe(true));
    expect(screen.getByRole('button', { name: /تصدير CSV/ })).toBeTruthy();
  });

  it('keeps access-change records hidden when global access-management scope is absent', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<AuditPage access={{ ...access, all_companies: false, company_ids: ['one-company'] }} range={range} />);
    expect(await screen.findByRole('tab', { name: /الأحداث/ })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: /تغييرات الوصول/ })).toBeNull();
    expect(vi.mocked(api).mock.calls.some(([path]) => typeof path === 'string' && path.includes('/access-changes'))).toBe(false);
  });
});
