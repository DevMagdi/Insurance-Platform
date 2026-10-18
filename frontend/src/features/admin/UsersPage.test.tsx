import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { UsersPage } from './UsersPage';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: JSON.stringify }));
const access: StaffAccess = { id: 'admin', full_name: 'Admin', email: 'admin@company.com', status: 'active', token_version: 0, two_factor_enabled: false, two_factor_required: false, roles: [], overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [], permissions: ['users.view', 'users.create', 'users.edit'] };
const departments = [{ id: 'claims', name: 'التعويضات' }, { id: 'operations', name: 'العمليات' }];
const employee = { id: 'employee', full_name: 'Mona Ali', email: 'mona@company.com', status: 'active', department_id: 'claims', department_name: 'التعويضات', avatar_url: '/api/staff/users/employee/avatar', two_factor_enabled: false, two_factor_required: false };
const profile = { ...employee, personal: { national_id: '29901010112345', date_of_birth: '1999-01-01', mobile: '+201123456789', address: '12 Nile Street', city: 'Giza', job_title: 'Claims specialist', emergency_phone: '+201012345678' } };

function install() {
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === '/api/staff/users') return [employee] as never;
    if (path === '/api/staff/departments') return departments as never;
    if (path === '/api/staff/users/employee/profile') return profile as never;
    return {} as never;
  });
}
afterEach(() => vi.clearAllMocks());

describe('employee directory', () => {
  it('requires an explicit department and sends invitations to the entered real address', async () => {
    install();
    render(<UsersPage access={access} onAccessChanged={vi.fn()} />);
    await screen.findByText('Mona Ali');
    fireEvent.click(screen.getByRole('button', { name: 'دعوة مستخدم' }));
    const modal = screen.getByRole('dialog');
    fireEvent.change(within(modal).getByLabelText('الاسم الكامل'), { target: { value: 'New Employee' } });
    fireEvent.change(within(modal).getByLabelText('البريد الإلكتروني'), { target: { value: 'new.employee@gmail.com' } });
    expect(within(modal).getByRole<HTMLButtonElement>('button', { name: 'إرسال الدعوة' }).disabled).toBe(true);
    fireEvent.change(within(modal).getByRole('combobox', { name: 'قسم المستخدم الجديد' }), { target: { value: 'claims' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'إرسال الدعوة' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.find(([path]) => path === '/api/staff/users/invitations')).toEqual(['/api/staff/users/invitations', { method: 'POST', body: JSON.stringify({ email: 'new.employee@gmail.com', full_name: 'New Employee', department_id: 'claims' }) }]));
    await screen.findByRole('status');
  });

  it('opens the complete profile by clicking the photo and persists department changes', async () => {
    install();
    render(<UsersPage access={access} onAccessChanged={vi.fn()} />);
    const open = await screen.findByRole('button', { name: 'عرض بروفايل Mona Ali' });
    expect(within(open).getByAltText('صورة Mona Ali').getAttribute('src')).toBe(employee.avatar_url);
    fireEvent.click(open);
    const modal = screen.getByRole('dialog');
    await within(modal).findByText('29901010112345');
    expect(within(modal).getByText('12 Nile Street')).toBeTruthy();
    expect(within(modal).getByText('+201123456789')).toBeTruthy();
    expect(within(modal).getByText('Claims specialist')).toBeTruthy();
    fireEvent.change(within(modal).getByRole('combobox', { name: 'قسم الموظف' }), { target: { value: 'operations' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'حفظ القسم' }));
    await within(modal).findByText('تم حفظ القسم.');
    expect(vi.mocked(api).mock.calls.find(([path]) => path === '/api/staff/users/employee/department')).toEqual(['/api/staff/users/employee/department', { method: 'PATCH', body: JSON.stringify({ department_id: 'operations' }) }]);
  });

  it('never loads staff data for a user without users.view', () => {
    render(<UsersPage access={{ ...access, permissions: [] }} onAccessChanged={vi.fn()} />);
    expect(screen.getByText('إدارة المستخدمين غير متاحة')).toBeTruthy();
    expect(api).not.toHaveBeenCalled();
  });
});
