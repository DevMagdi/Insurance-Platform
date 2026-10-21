import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { UsersPage } from './UsersPage';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: JSON.stringify }));
const access: StaffAccess = { id: 'admin', full_name: 'Admin', email: 'admin@company.com', status: 'active', token_version: 0, two_factor_enabled: false, two_factor_required: false, roles: [], overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [], permissions: ['users.view', 'users.create', 'users.edit'] };
const departments = [
  { id: 'claims', name: 'التعويضات', description: 'مطالبات العملاء', is_active: true, employee_count: 1 },
  { id: 'operations', name: 'العمليات', description: '', is_active: true, employee_count: 0 },
];
const employee = { id: 'employee', full_name: 'Mona Ali', email: 'mona@company.com', status: 'active', department_id: 'claims', department_name: 'التعويضات', avatar_url: '/api/staff/users/employee/avatar', two_factor_enabled: false, two_factor_required: false };
const profile = { ...employee, department_description: 'مطالبات العملاء', created_at: '2026-09-01T08:00:00Z', last_login_at: '2026-10-18T08:00:00Z', two_factor_enabled: true, two_factor_required: true, profile_completed_at: '2026-10-01T08:00:00Z', roles: [{ id: 'claims-role', name: 'أخصائي التعويضات', description: 'متابعة المطالبات', is_system: false }], permissions: ['complaints.view', 'complaints.edit'], personal: { national_id: '29901010112345', date_of_birth: '1999-01-01', mobile: '+201123456789', address: '12 Nile Street', city: 'Giza', job_title: 'Claims specialist', emergency_phone: '+201012345678' } };

function install() {
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === '/api/staff/users') return [employee] as never;
    if (path === '/api/staff/departments') return departments as never;
    if (path === '/api/staff/users/employee/profile') return profile as never;
    if (path === '/api/staff/users/revision') return { revision: 'revision-1' } as never;
    return {} as never;
  });
}
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });

describe('employee directory liveness', () => {
  it('refetches the directory when the cheap revision signature moves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    let revision = 'revision-1';
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === '/api/staff/users') return [employee] as never;
      if (path === '/api/staff/departments') return departments as never;
      if (path === '/api/staff/users/revision') return { revision } as never;
      return {} as never;
    });
    render(<UsersPage access={access} onAccessChanged={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Mona Ali')).toBeTruthy();
    const directoryCalls = () => vi.mocked(api).mock.calls.filter(([path]) => path === '/api/staff/users').length;
    expect(directoryCalls()).toBe(1);

    // A steady signature must not re-download the directory.
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(directoryCalls()).toBe(1);

    // An accepted invitation / deactivation moves the signature: the rows are
    // refetched without anyone pressing refresh.
    revision = 'revision-2';
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(directoryCalls()).toBe(2);
    expect(screen.getByText('Mona Ali')).toBeTruthy();
  });

  it('ignores an empty signature so scoped staff never poll-refetch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === '/api/staff/users') return [] as never;
      if (path === '/api/staff/departments') return [] as never;
      if (path === '/api/staff/users/revision') return { revision: '' } as never;
      return {} as never;
    });
    render(<UsersPage access={access} onAccessChanged={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('لا يوجد مستخدمون مطابقون')).toBeTruthy();
    const directoryCalls = () => vi.mocked(api).mock.calls.filter(([path]) => path === '/api/staff/users').length;
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    expect(directoryCalls()).toBe(1);
  });
});

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
    expect(within(modal).getByText('أخصائي التعويضات')).toBeTruthy();
    expect(within(modal).getByText('مطالبات العملاء')).toBeTruthy();
    expect(within(modal).getByText('عرض الطلبات')).toBeTruthy();
    expect(within(modal).getByText('مفعّلة · إلزامية')).toBeTruthy();
    expect(within(modal).getByText('معلومات الحساب')).toBeTruthy();
    fireEvent.change(within(modal).getByRole('combobox', { name: 'قسم الموظف' }), { target: { value: 'operations' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'حفظ القسم' }));
    await within(modal).findByText('تم حفظ القسم.');
    expect(vi.mocked(api).mock.calls.find(([path]) => path === '/api/staff/users/employee/department')).toEqual(['/api/staff/users/employee/department', { method: 'PATCH', body: JSON.stringify({ department_id: 'operations' }) }]);
  });

  it('creates and edits a department, filters employees and excludes inactive choices from invitations', async () => {
    const catalogue = [...departments];
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path === '/api/staff/departments' && init?.method === 'POST') {
        catalogue.push({ id: 'support', ...JSON.parse(init.body as string), employee_count: 0 });
        return catalogue.at(-1) as never;
      }
      if (path === '/api/staff/departments/support' && init?.method === 'PUT') {
        Object.assign(catalogue[2], JSON.parse(init.body as string));
        return catalogue[2] as never;
      }
      if (path === '/api/staff/departments') return [...catalogue] as never;
      if (path === '/api/staff/users') return [employee] as never;
      return profile as never;
    });
    render(<UsersPage access={access} onAccessChanged={vi.fn()} />);
    await screen.findByText('Mona Ali');
    fireEvent.click(screen.getByRole('button', { name: 'قسم جديد' }));
    let modal = screen.getByRole('dialog');
    fireEvent.change(within(modal).getByLabelText('اسم القسم'), { target: { value: 'خدمة العملاء' } });
    fireEvent.change(within(modal).getByLabelText('الوصف (اختياري)'), { target: { value: 'دعم العملاء' } });
    fireEvent.click(within(modal).getByRole('checkbox', { name: /قسم مفعّل/ }));
    fireEvent.click(within(modal).getByRole('button', { name: 'إنشاء القسم' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.find(([path, init]) => path === '/api/staff/departments' && init?.method === 'POST')).toEqual([
      '/api/staff/departments', { method: 'POST', body: JSON.stringify({ name: 'خدمة العملاء', description: 'دعم العملاء', is_active: false }) },
    ]));
    const edit = await screen.findByRole('button', { name: 'تعديل قسم خدمة العملاء' });
    fireEvent.click(edit);
    modal = screen.getByRole('dialog');
    expect((within(modal).getByLabelText('اسم القسم') as HTMLInputElement).value).toBe('خدمة العملاء');
    fireEvent.change(within(modal).getByLabelText('الوصف (اختياري)'), { target: { value: 'مركز الدعم' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'حفظ التعديلات' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, init]) => path === '/api/staff/departments/support' && init?.method === 'PUT' && JSON.parse(init.body as string).description === 'مركز الدعم')).toBe(true));
    fireEvent.change(screen.getByRole('combobox', { name: 'تصفية المستخدمين حسب القسم' }), { target: { value: 'support' } });
    expect(screen.getByText('لا يوجد مستخدمون مطابقون')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'دعوة مستخدم' }));
    modal = screen.getByRole('dialog');
    expect(within(modal).getByRole('combobox', { name: 'قسم المستخدم الجديد' }).querySelector('option[value="support"]')).toBeNull();
  });

  it('hides department mutations when staff only has directory-view permission', async () => {
    install();
    render(<UsersPage access={{ ...access, permissions: ['users.view'] }} onAccessChanged={vi.fn()} />);
    await screen.findByRole('heading', { name: 'الأقسام' });
    expect(screen.queryByRole('button', { name: 'قسم جديد' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'تعديل قسم التعويضات' })).toBeNull();
  });

  it('never loads staff data for a user without users.view', () => {
    render(<UsersPage access={{ ...access, permissions: [] }} onAccessChanged={vi.fn()} />);
    expect(screen.getByText('إدارة المستخدمين غير متاحة')).toBeTruthy();
    expect(api).not.toHaveBeenCalled();
  });
});
