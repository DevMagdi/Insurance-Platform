import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  it('requires global scope and directory visibility for team management', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    const { rerender } = render(<OperationsPage
      access={{ ...access, permissions: ['teams.manage', 'users.view'], all_companies: false, company_ids: ['co-1'], ownership: 'team' }}
      range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }}
      inScopeCompanies={scopedCompanies}
    />);
    expect(await screen.findByText('إعدادات الإدارة غير متاحة')).toBeTruthy();
    expect(screen.queryByRole('tab', { name: /الفرق/ })).toBeNull();

    rerender(<OperationsPage
      access={{ ...access, permissions: ['teams.manage', 'users.view'] }}
      range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }}
      inScopeCompanies={scopedCompanies}
    />);
    expect(await screen.findByRole('tab', { name: /الفرق/ })).toBeTruthy();
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

  it('saves scheduled reports with relative filters, format and the main API contract', async () => {
    const reportAccess: StaffAccess = {
      ...access,
      permissions: ['reports.manage', 'analytics.view', 'analytics.export'],
    };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/reports/schedules') return [] as never;
      return { id: 'report-1' } as never;
    });
    render(<OperationsPage access={reportAccess} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /التقارير المجدولة/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /تعريف تقرير جديد/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'اسم التقرير' }), { target: { value: 'Weekly review' } });
    fireEvent.change(screen.getByRole('textbox', { name: /المستلمون/ }), { target: { value: 'ops@example.com' } });
    fireEvent.change(screen.getByPlaceholderText('0 8 * * 1'), { target: { value: '0 8 * * 1' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'المنطقة الزمنية' }), { target: { value: 'Africa/Cairo' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'نافذة البيانات' }), { target: { value: '30' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'صيغة التقرير' }), { target: { value: 'xlsx' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));

    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/reports/schedules'
      && options?.method === 'POST'
      && JSON.parse(String(options.body)).filters.window_days === 30
      && JSON.parse(String(options.body)).format === 'xlsx'
      && !('date_from' in JSON.parse(String(options.body)).filters),
    )).toBe(true));
  });
  it('keeps the team leader inside the member list', async () => {
    const teamAccess: StaffAccess = { ...access, permissions: ['teams.manage', 'users.view'] };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/admin/teams') return [] as never;
      if (path === '/api/staff/users') return [
        { id: 'agent-1', email: 'agent@example.test', full_name: 'Mona Ali', status: 'active', two_factor_enabled: false, two_factor_required: false },
      ] as never;
      return { id: 'team-1' } as never;
    });
    render(<OperationsPage access={teamAccess} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /الفرق/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /الفرق/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'إضافة الفرق' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'اسم الفريق' }), { target: { value: 'فريق claims' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'قائد الفريق' }), { target: { value: 'agent-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/admin/teams'
      && options?.method === 'POST'
      && JSON.parse(String(options.body)).member_ids.includes('agent-1'),
    )).toBe(true));
  });

  it('refuses an SLA rule whose resolution is shorter than its response', async () => {
    const slaAccess: StaffAccess = { ...access, permissions: ['sla.manage'] };
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={slaAccess} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /قواعد SLA/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /قواعد SLA/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'إضافة قواعد SLA' }));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'زمن الاستجابة (دقيقة)' }), { target: { value: '120' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: /زمن الحل/ }), { target: { value: '60' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));
    // A resolution shorter than the response never reaches the API: the input
    // refuses the value and the form is not submittable.
    expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/admin/sla-rules' && options?.method === 'POST')).toBe(false);

    fireEvent.change(screen.getByRole('spinbutton', { name: /زمن الحل/ }), { target: { value: '240' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/admin/sla-rules' && options?.method === 'POST')).toBe(true));
  });

  it('refuses an invalid contact address for a company', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={access} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /شركات التأمين/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'إضافة شركات التأمين' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'المعرّف المختصر' }), { target: { value: 'nile-assurance' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'الاسم بالإنجليزية' }), { target: { value: 'Nile Assurance' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'الاسم بالعربية' }), { target: { value: 'تأمين النيل' } });
    fireEvent.change(screen.getByRole('textbox', { name: /عناوين البريد للتواصل/ }), { target: { value: 'broken-address' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('عنوان البريد غير صالح'));
    expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/admin/companies' && options?.method === 'POST')).toBe(false);
  });

  it('refuses duplicate report recipients', async () => {
    const reportAccess: StaffAccess = {
      ...access,
      permissions: ['reports.manage', 'analytics.view', 'analytics.export'],
    };
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={reportAccess} range={{ from: '2026-10-01', to: '2026-10-15', preset: 'custom' }} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByRole('tab', { name: /التقارير المجدولة/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /تعريف تقرير جديد/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'اسم التقرير' }), { target: { value: 'Weekly review' } });
    fireEvent.change(screen.getByRole('textbox', { name: /المستلمون/ }), { target: { value: 'ops@example.com, ops@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الإعداد' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('مكرر'));
    expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/reports/schedules' && options?.method === 'POST')).toBe(false);
  });
});

const range = { from: '2026-10-01', to: '2026-10-15', preset: 'custom' } as const;
const reportAccess: StaffAccess = { ...access, permissions: ['reports.manage', 'analytics.view', 'analytics.export'] };
const teamAccess: StaffAccess = { ...access, permissions: ['teams.manage', 'users.view'] };

/**
 * Every case below reproduces a real console crash: the table used to cast the
 * fetched payload straight into a record type, so one missing field (`filters`,
 * `recipient_emails`, `supported_types`, `member_ids`) threw a TypeError and
 * blanked the whole Operations page.
 */
describe('OperationsPage never crashes on incomplete data', () => {
  it('renders scheduled reports whose fields are missing', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/reports/schedules') return [
        { id: 'r-1', name: 'تقرير ناقص' }, // no filters, no recipient_emails
        { id: 'r-2' }, // no name either
        null, // not even an object
      ] as never;
      return [] as never;
    });
    render(<OperationsPage access={reportAccess} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('tab', { name: /التقارير المجدولة/ }));
    expect(await screen.findByText('تقرير ناقص')).toBeTruthy();
    // The absent window falls back to 7 days and the absent recipients to a dash.
    expect(screen.getAllByText(/7 يوم/, { selector: 'small' }).length).toBe(2);
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // The unusable row is reported instead of being rendered as `undefined`.
    expect(screen.getByText(/تم إخفاء/)).toBeTruthy();
  });

  it('renders a full schedule row exactly as the API returns it', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/reports/schedules') return [{
        id: 'r-1', name: 'تقرير أسبوعي', filters: { window_days: 30, company_id: null, insurance_type: null },
        cron_expression: '0 8 * * 1', time_zone: 'Africa/Cairo', recipient_emails: ['ops@example.com', 'lead@example.com'],
        format: 'xlsx', is_active: true, next_run_at: '2026-10-19T06:00:00Z', last_run_at: null, last_state: null,
        created_at: '2026-10-01T08:00:00Z',
      }] as never;
      return [] as never;
    });
    render(<OperationsPage access={reportAccess} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('tab', { name: /التقارير المجدولة/ }));
    expect(await screen.findByText('تقرير أسبوعي')).toBeTruthy();
    expect(screen.getByText(/30 يوم/, { selector: 'small' })).toBeTruthy();
    expect(screen.getByText('ops@example.com, lead@example.com')).toBeTruthy();
    expect(screen.getByText('0 8 * * 1')).toBeTruthy();
    expect(screen.queryByText(/تم إخفاء/)).toBeNull();
  });

  it('shows the empty state when the API returns no schedules', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={reportAccess} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('tab', { name: /التقارير المجدولة/ }));
    expect(await screen.findByText('لا توجد إعدادات بعد')).toBeTruthy();
  });

  it('shows the empty state when the schedules endpoint fails', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/reports/schedules') throw new Error('تعذر تحميل التقارير.');
      return [] as never;
    });
    render(<OperationsPage access={reportAccess} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('tab', { name: /التقارير المجدولة/ }));
    expect(await screen.findByText('لا توجد إعدادات بعد')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('تعذر تحميل التقارير.');
  });

  it('renders companies without list fields', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => (path === '/api/staff/admin/companies'
      ? ([{ id: 'co-1', slug: 'nile', name_en: 'Nile', name_ar: 'تأمين النيل' }, { id: 'co-2' }] as never)
      : ([] as never)));
    render(<OperationsPage access={access} range={range} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByText('تأمين النيل')).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('renders teams without a member list or a leader', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => (path === '/api/staff/admin/teams'
      ? ([{ id: 'team-1', name: 'فريق المطالبات' }, { id: 'team-2', name: 'فريق ثانٍ', member_ids: 'not-an-array' }] as never)
      : ([] as never)));
    render(<OperationsPage access={teamAccess} range={range} inScopeCompanies={scopedCompanies} />);
    expect(await screen.findByText('فريق المطالبات')).toBeTruthy();
    expect(screen.getByText('فريق ثانٍ')).toBeTruthy();
    expect(screen.getAllByText(/أعضاء/, { selector: 'span.count-pill' }).length).toBe(2);
  });

  it('never renders a stale response from another tab', async () => {
    // The reported crash: the companies response landed after the operator had
    // already switched to the reports tab, and the reports renderer then read
    // `recipient_emails` (and `filters.window_days` before it) off a company row.
    let releaseCompanies: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { releaseCompanies = resolve; });
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === '/api/staff/admin/companies') {
        await gate;
        return [{ id: 'co-9', slug: 'late', name_en: 'Late', name_ar: 'شركة متأخرة', contact_emails: [], supported_types: ['health'], is_active: true }] as never;
      }
      if (path === '/api/staff/reports/schedules') return [{
        id: 'r-1', name: 'تقرير أسبوعي', filters: { window_days: 30 }, cron_expression: '0 8 * * 1',
        time_zone: 'Africa/Cairo', recipient_emails: ['ops@example.com'], format: 'csv', is_active: true,
        next_run_at: null, last_run_at: null, last_state: null, created_at: '2026-10-01T08:00:00Z',
      }] as never;
      return [] as never;
    });
    render(<OperationsPage
      access={{ ...access, permissions: ['companies.manage', 'reports.manage', 'analytics.view', 'analytics.export'] }}
      range={range}
      inScopeCompanies={scopedCompanies}
    />);
    fireEvent.click(await screen.findByRole('tab', { name: /التقارير المجدولة/ }));
    expect(await screen.findByText('تقرير أسبوعي')).toBeTruthy();
    releaseCompanies();
    await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/api/staff/admin/companies').length).toBeGreaterThan(0));
    await new Promise((resolve) => { setTimeout(resolve, 30); });
    // The late companies payload must not reach the reports table.
    expect(screen.queryByText('شركة متأخرة')).toBeNull();
    expect(screen.getByText('ops@example.com')).toBeTruthy();
  });

  it('refuses a company slug the API would reject', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={access} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('button', { name: /إضافة شركات التأمين/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'المعرّف المختصر' }), { target: { value: 'Nile Assurance' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'الاسم بالإنجليزية' }), { target: { value: 'Nile Assurance' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'الاسم بالعربية' }), { target: { value: 'تأمين النيل' } });
    fireEvent.submit(screen.getByRole('textbox', { name: 'المعرّف المختصر' }).closest('form') as HTMLFormElement);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('المعرّف المختصر'));
    expect(vi.mocked(api).mock.calls.some(([path, options]) =>
      path === '/api/staff/admin/companies' && options?.method === 'POST')).toBe(false);
  });

  it('keeps an HTML pattern the browser can compile (unicodeSets `v` flag)', async () => {
    vi.mocked(api).mockResolvedValue([] as never);
    render(<OperationsPage access={access} range={range} inScopeCompanies={scopedCompanies} />);
    fireEvent.click(await screen.findByRole('button', { name: /إضافة شركات التأمين/ }));
    const slug = screen.getByRole('textbox', { name: 'المعرّف المختصر' }) as HTMLInputElement;
    expect(slug.pattern).toBeTruthy();
    // Browsers compile `pattern` as `new RegExp('^(?:' + pattern + ')$', 'v')`;
    // an unescaped `/` or `-` inside a class throws "Invalid regular expression".
    const compiled = new RegExp(`^(?:${slug.pattern})$`, 'v');
    expect(compiled.test('nile-assurance')).toBe(true);
    expect(compiled.test('Nile')).toBe(false);
  });
});
