import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess, StaffDirectoryEntry } from '../../types';
import { AccessEditor } from './AccessEditor';

vi.mock('../../api', () => ({ api: vi.fn(), jsonBody: JSON.stringify }));
const user: StaffDirectoryEntry = { id: 'employee', email: 'staff@example.test', full_name: 'Mona Ali', status: 'active', two_factor_enabled: false, two_factor_required: false };
const snapshot: StaffAccess = {
  id: user.id, email: user.email, full_name: user.full_name, status: 'active', token_version: 1,
  two_factor_enabled: false, two_factor_required: false, roles: ['claims'],
  permissions: ['complaints.view'], overrides: {}, all_companies: true, company_ids: [],
  insurance_types: [], ownership: 'all', team_ids: [],
};
const options = {
  roles: [
    { id: 'claims', name: 'أخصائي التعويضات', description: 'متابعة الشكاوى', is_system: false, permissions: ['complaints.view'] },
    { id: 'support', name: 'خدمة العملاء', description: 'خدمة الفريق', is_system: false, permissions: ['users.view'] },
  ],
  companies: [
    { id: 'one', name_ar: 'شركة النيل', name_en: 'Nile' },
    { id: 'two', name_ar: 'شركة الأمان', name_en: 'Aman' },
  ],
  teams: [{ id: 'team', name: 'فريق المتابعة', member_ids: [] }],
};

afterEach(() => vi.clearAllMocks());

describe('access editor', () => {
  it('edits roles, company scope, overrides and preserves the exact access payload', async () => {
    vi.mocked(api).mockImplementation(async (path) => {
      if (path.endsWith('/access/explain')) return { editable: true, access: snapshot, why: {} } as never;
      if (path.endsWith('/access')) return snapshot as never;
      if (path.endsWith('/access-options')) return options as never;
      return {} as never;
    });
    const onSaved = vi.fn();
    render(<AccessEditor user={user} onClose={vi.fn()} onSaved={onSaved} />);
    const dialog = screen.getByRole('dialog');
    await within(dialog).findByRole('heading', { name: 'مصفوفة الصلاحيات' });
    expect(within(dialog).getByRole('heading', { name: 'نطاق الشركات والملكية' })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /خدمة العملاء/ }));
    fireEvent.click(within(dialog).getByRole('radio', { name: /شركات محددة/ }));
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'شركة الأمان' }));
    fireEvent.change(within(dialog).getByLabelText('البحث في الصلاحيات'), { target: { value: 'تعديل بيانات المستخدم' } });
    expect(within(dialog).queryByRole('combobox', { name: 'تجاوز عرض الشكاوى' })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'منع الكل' }));
    expect((within(dialog).getByRole('combobox', { name: 'تجاوز تعديل بيانات المستخدم' }) as HTMLSelectElement).value).toBe('deny');
    fireEvent.click(within(dialog).getByRole('button', { name: /عرض كـ Mona/ }));
    expect(screen.getAllByRole('dialog')).toHaveLength(2);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole('button', { name: 'حفظ الوصول' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(vi.mocked(api).mock.calls.find(([path, init]) => path === '/api/staff/users/employee/access' && init?.method === 'PUT')).toEqual([
      '/api/staff/users/employee/access', {
        method: 'PUT',
        body: JSON.stringify({ role_ids: ['claims', 'support'], allow: [], deny: ['users.edit'], all_companies: false, company_ids: ['one', 'two'], insurance_types: [], ownership: 'all', team_ids: [] }),
      },
    ]);
  });
  it('never lets the company scope become empty', async () => {
    vi.mocked(api).mockImplementation(async (path) => {
      if (path.endsWith('/access/explain')) return { editable: true, access: snapshot, why: {} } as never;
      if (path.endsWith('/access')) return snapshot as never;
      if (path.endsWith('/access-options')) return options as never;
      return {} as never;
    });
    const onSaved = vi.fn();
    render(<AccessEditor user={user} onClose={vi.fn()} onSaved={onSaved} />);
    const dialog = screen.getByRole('dialog');
    await within(dialog).findByRole('heading', { name: 'مصفوفة الصلاحيات' });
    // "شركات محددة" selects a company straight away: the API rejects an empty
    // company list together with all_companies=false.
    fireEvent.click(within(dialog).getByRole('radio', { name: /شركات محددة/ }));
    await waitFor(() => expect((within(dialog).getByRole('checkbox', { name: 'شركة النيل' }) as HTMLInputElement).checked).toBe(true));
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'شركة النيل' }));
    // The last company stays selected: removing it would empty the scope.
    expect((within(dialog).getByRole('checkbox', { name: 'شركة النيل' }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'حفظ الوصول' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    const call = vi.mocked(api).mock.calls.find(([path, init]) => path === '/api/staff/users/employee/access' && init?.method === 'PUT');
    expect(JSON.parse(String(call?.[1]?.body)).company_ids).toEqual(['one']);
  });
});
