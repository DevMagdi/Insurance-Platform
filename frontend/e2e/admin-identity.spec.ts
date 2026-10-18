import { expect, test, type Page, type Route } from '@playwright/test';

const employeeId = '55555555-5555-4555-8555-555555555555';
const adminAccess = {
  id: '44444444-4444-4444-8444-444444444444', full_name: 'Platform Admin', email: 'admin@example.test',
  status: 'active', token_version: 1, two_factor_enabled: true, two_factor_required: true,
  roles: ['admin'], permissions: ['users.view', 'users.create', 'users.edit', 'users.manage_access', 'analytics.view', 'complaints.view'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

async function install(page: Page) {
  const state = {
    departments: [{ id: 'claims', name: 'التعويضات', description: 'مطالبات العملاء', is_active: true, employee_count: 1 }],
    created: null as Record<string, unknown> | null,
    savedAccess: null as Record<string, unknown> | null,
    departmentId: 'claims',
  };
  await page.addInitScript(() => { localStorage.setItem('insurance-portal:lang', 'ar'); document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax'; });
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const department = state.departments.find((item) => item.id === state.departmentId) ?? state.departments[0];
    const employee = { id: employeeId, full_name: 'Mona Ali', email: 'mona@company.com', status: 'active', department_id: department.id, department_name: department.name, avatar_url: null, two_factor_enabled: true, two_factor_required: false, last_login_at: '2026-10-18T08:00:00Z' };
    const access = { ...adminAccess, ...employee, roles: ['claims-role'], permissions: ['complaints.view'], overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [] };
    if (path === '/api/me') return json({ kind: 'staff', access: adminAccess });
    if (path === '/api/auth/csrf') return json({ available: true });
    if (path === '/api/public/config') return json({ captcha: null, privacy_policy_version: '2026-10' });
    if (path === '/api/me/notifications') return json([]);
    if (path === '/api/staff/departments' && request.method() === 'POST') {
      state.created = request.postDataJSON();
      state.departments.push({ id: 'support', ...state.created, employee_count: 0 } as typeof department);
      return json({ id: 'support', ...state.created, employee_count: 0 }, 201);
    }
    if (path === '/api/staff/departments') return json(state.departments);
    if (path === '/api/staff/departments/support' && request.method() === 'PUT') {
      Object.assign(state.departments[1], request.postDataJSON());
      return json(state.departments[1]);
    }
    if (path === '/api/staff/users') return json([employee]);
    if (path === `/api/staff/users/${employeeId}/profile`) return json({ ...employee,
      department_description: department.description, department_is_active: department.is_active,
      created_at: '2026-09-01T08:00:00Z', profile_completed_at: '2026-09-03T08:00:00Z',
      roles: [{ id: 'claims-role', name: 'أخصائي التعويضات', description: 'متابعة المطالبات', is_system: false }],
      permissions: ['complaints.view', 'complaints.edit'],
      personal: { full_name: 'Mona Ali', national_id: '29901010112345', date_of_birth: '1999-01-01', mobile: '+201123456789', city: 'Giza', address: '12 Nile Street', job_title: 'Claims specialist', emergency_phone: null },
    });
    if (path === `/api/staff/users/${employeeId}/department` && request.method() === 'PATCH') {
      state.departmentId = request.postDataJSON().department_id;
      return json({ id: employeeId, department_id: state.departmentId });
    }
    if (path === `/api/staff/users/${employeeId}/access/explain`) return json({ editable: true, access, why: {} });
    if (path === `/api/staff/users/${employeeId}/access` && request.method() === 'PUT') {
      state.savedAccess = request.postDataJSON();
      return json({ id: employeeId, token_version: 2 });
    }
    if (path === `/api/staff/users/${employeeId}/access`) return json(access);
    if (path === '/api/staff/admin/access-options') return json({ companies: [{ id: 'one', name_ar: 'شركة النيل', name_en: 'Nile' }], teams: [{ id: 'team', name: 'فريق المتابعة', member_ids: [] }], roles: [{ id: 'claims-role', name: 'أخصائي التعويضات', description: 'متابعة المطالبات', is_system: false, permissions: ['complaints.view'] }] });
    if (path === '/api/staff/analytics/overview') return json({ kpis: { total: 0, new: 0, reopened: 0, resolved: 0, sla_breaches: 0 }, time_series: [], by_status: [], by_company: [] });
    if (path === '/api/staff/complaints/filter-options') return json({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] });
    return json([]);
  });
  return state;
}

async function expectNoHorizontalOverflow(page: Page, selector: string) {
  const dimensions = await page.locator(selector).evaluate((element) => ({ width: element.clientWidth, scroll: element.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 2);
}

test('admin creates and activates a department, assigns an employee and filters the directory', async ({ page }) => {
  const state = await install(page);
  await page.goto('/staff/admin');
  await page.getByRole('button', { name: 'قسم جديد' }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByLabel('اسم القسم').fill('خدمة العملاء');
  await dialog.getByLabel('الوصف (اختياري)').fill('متابعة الفريق');
  await dialog.getByRole('checkbox', { name: /قسم مفعّل/ }).uncheck();
  await dialog.getByRole('button', { name: 'إنشاء القسم' }).click();
  await expect(page.getByRole('button', { name: 'تعديل قسم خدمة العملاء' })).toBeVisible();
  expect(state.created).toEqual({ name: 'خدمة العملاء', description: 'متابعة الفريق', is_active: false });
  await page.getByRole('button', { name: 'تعديل قسم خدمة العملاء' }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('checkbox', { name: /قسم مفعّل/ }).check();
  await dialog.getByRole('button', { name: 'حفظ التعديلات' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText('متابعة الفريق')).toBeVisible();
  await page.goto('/staff/users');
  await page.getByRole('button', { name: 'عرض بروفايل Mona Ali' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: 'الأدوار والصلاحيات' })).toBeVisible();
  await expect(dialog.getByText('أخصائي التعويضات')).toBeVisible();
  await expect(dialog.getByText('29901010112345')).toBeVisible();
  await expect(dialog.getByText('معلومات الحساب')).toBeVisible();
  await expectNoHorizontalOverflow(page, '.staff-profile-dialog .modal-content');
  await dialog.getByRole('combobox', { name: 'قسم الموظف' }).selectOption('support');
  await dialog.getByRole('button', { name: 'حفظ القسم' }).click();
  await expect(dialog.getByText('تم حفظ القسم.')).toBeVisible();
  await dialog.getByRole('button', { name: 'إغلاق' }).click();
  await page.getByRole('combobox', { name: 'تصفية المستخدمين حسب القسم' }).selectOption('support');
  await expect(page.getByRole('button', { name: 'عرض بروفايل Mona Ali' })).toBeVisible();
});

test('RTL access editor has no horizontal overlap on desktop and mobile and saves an override', async ({ page }) => {
  const state = await install(page);
  await page.goto('/staff/users');
  await page.getByRole('button', { name: 'تحرير وصول Mona Ali' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: 'مصفوفة الصلاحيات' })).toBeVisible();
  await expect(dialog.getByRole('heading', { name: 'نطاق الشركات والملكية' })).toBeVisible();
  await expectNoHorizontalOverflow(page, '.access-editor-dialog .modal-content');
  await dialog.getByLabel('البحث في الصلاحيات').fill('عرض الشكاوى');
  await dialog.getByRole('combobox', { name: 'تجاوز عرض الشكاوى' }).selectOption('deny');
  await expect(dialog.getByText('موقوفة')).toBeVisible();
  await dialog.getByRole('button', { name: 'عرض كـ Mona' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await dialog.getByRole('button', { name: 'حفظ الوصول' }).click();
  await expect.poll(() => state.savedAccess?.deny).toEqual(['complaints.view']);
});
