import { expect, test, type Page, type Route } from '@playwright/test';

// Browser routes/forms are real; API is mocked. Actual PostgreSQL + SMTP socket
// acceptance, authorization and encrypted storage are covered by backend integration tests.
const userId = '55555555-5555-4555-8555-555555555555';
const departmentId = '66666666-6666-4666-8666-666666666666';
const token = 'invitation-token-with-at-least-forty-three-characters';
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
const email = 'new.employee@gmail.com';
const password = 'Cedar-River-Garden-49';
const access = { id: 'admin', full_name: 'Administrator', email: 'admin@company.com', status: 'active', token_version: 0, two_factor_enabled: true, two_factor_required: false, roles: [], overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [], permissions: ['users.view', 'users.create', 'users.edit', 'analytics.view', 'complaints.view'] };

async function install(page: Page, kind: 'invitation' | 'password_reset' | 'expired' = 'invitation') {
  const state = { accepted: false, invite: null as null | Record<string, string>, profile: null as null | Record<string, string> };
  await page.addInitScript(() => { localStorage.setItem('insurance-portal:lang', 'ar'); document.cookie = 'csrf_token=e2e-session; Path=/; SameSite=Lax'; });
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const employee = { id: userId, full_name: state.profile?.full_name ?? 'New Employee', email, status: state.accepted ? 'active' : 'invited', department_id: departmentId, department_name: 'التعويضات', avatar_url: state.accepted ? `/api/staff/users/${userId}/avatar` : null, two_factor_enabled: false, two_factor_required: false, profile_completed_at: state.accepted ? '2026-10-18T10:00:00Z' : null };
    if (path === '/api/me') return json({ kind: 'staff', access });
    if (path === '/api/auth/csrf') return json({ available: true });
    if (path === '/api/public/config') return json({ captcha: null, privacy_policy_version: '2026-10' });
    if (path === '/api/me/notifications') return json([]);
    if (path === '/api/staff/departments') return json([{ id: departmentId, name: 'التعويضات' }]);
    if (path === '/api/staff/users') return json([employee]);
    if (path === '/api/staff/users/invitations') {
      state.invite = request.postDataJSON();
      return json({ id: userId, message: 'Provider accepted the invitation' }, 202);
    }
    if (path === '/api/auth/staff/invitation/inspect') {
      if (kind === 'expired') return json({ detail: 'Invalid or expired invitation' }, 400);
      expect(request.postDataJSON().token).toBe(token);
      return json({ email, full_name: 'New Employee', department_name: 'التعويضات', kind });
    }
    if (path === '/api/auth/staff/invitation/accept') {
      const body = request.postDataJSON();
      expect(body.token).toBe(token);
      expect(body.password).toBe(password);
      if (kind === 'invitation') {
        expect(body.avatar_base64).toBe(image.toString('base64'));
        expect(body.profile.department_id).toBeUndefined();
        state.profile = body.profile;
      } else { expect(body.profile).toBeUndefined(); expect(body.avatar_base64).toBeUndefined(); }
      state.accepted = true;
      return json({ next_step: 'sign_in_with_password' });
    }
    if (path === `/api/staff/users/${userId}/profile`) return json({ ...employee, personal: state.profile ? { ...state.profile, date_of_birth: '1999-01-01' } : null });
    if (path === `/api/staff/users/${userId}/avatar`) return route.fulfill({ contentType: 'image/png', body: image });
    if (path === '/api/staff/analytics/overview') return json({ kpis: { total: 0, new: 0, reopened: 0, resolved: 0, sla_breaches: 0 }, time_series: [], by_status: [], by_company: [] });
    if (path === '/api/staff/complaints/filter-options') return json({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] });
    return json([]);
  });
  return state;
}

test('department invitation → full profile/photo → clickable dashboard profile', async ({ page }) => {
  const state = await install(page);
  await page.goto('/staff/users');
  await page.getByRole('button', { name: 'دعوة مستخدم' }).click();
  const invite = page.getByRole('dialog');
  await invite.getByLabel('الاسم الكامل').fill('New Employee');
  await invite.getByLabel('البريد الإلكتروني').fill(email);
  await expect(invite.getByRole('button', { name: 'إرسال الدعوة' })).toBeDisabled();
  await invite.getByRole('combobox', { name: 'قسم المستخدم الجديد' }).selectOption(departmentId);
  await invite.getByRole('button', { name: 'إرسال الدعوة' }).click();
  await expect(invite).not.toBeVisible();
  expect(state.invite).toEqual({ email, full_name: 'New Employee', department_id: departmentId });

  await page.goto(`/invite#token=${token}`);
  await expect(page.getByRole('heading', { name: 'إنشاء الحساب والبروفايل' })).toBeVisible();
  await page.getByLabel('الاسم الكامل').fill('Mona Ahmed Ali');
  await page.getByLabel('الرقم القومي').fill('29901010112345');
  await page.getByLabel('رقم الموبايل', { exact: true }).fill('+201123456789');
  await page.getByLabel('العنوان', { exact: true }).fill('12 Nile Street, Dokki');
  await page.getByLabel('المدينة / المحافظة').fill('Giza');
  await page.getByLabel('المسمى الوظيفي (اختياري)').fill('Claims specialist');
  await page.getByLabel('هاتف الطوارئ (اختياري)').fill('+201012345678');
  await page.getByLabel(/^كلمة المرور/).fill(password);
  await page.getByLabel('تأكيد كلمة المرور').fill(password);
  await page.getByRole('button', { name: 'حفظ الحساب' }).click();
  expect(state.accepted).toBe(false); // native required-file validation
  await page.getByLabel(/^الصورة الشخصية/).setInputFiles({ name: 'portrait.png', mimeType: 'image/png', buffer: image });
  await expect(page.getByAltText('معاينة الصورة الشخصية')).toBeVisible();
  await expect(page).toHaveURL('/invite');
  await page.getByRole('button', { name: 'حفظ الحساب' }).click();
  await expect(page.getByRole('heading', { name: 'تم حفظ حسابك بنجاح' })).toBeVisible();
  await expect(page.locator('section[aria-live="polite"]').getByRole('link', { name: 'تسجيل الدخول', exact: true })).toHaveAttribute('href', '/staff/login');

  await page.goto('/staff/admin');
  const employee = page.getByRole('button', { name: 'عرض بروفايل Mona Ahmed Ali' });
  await expect(employee).toBeVisible();
  await expect(employee.getByAltText('صورة Mona Ahmed Ali')).toBeVisible();
  await expect(employee.getByText('التعويضات')).toBeVisible();
  await employee.click();
  const profile = page.getByRole('dialog');
  await expect(profile.getByText('29901010112345')).toBeVisible();
  await expect(profile.getByText('12 Nile Street, Dokki')).toBeVisible();
  await expect(profile.getByText('Claims specialist', { exact: true })).toBeVisible();
  await expect(profile.getByText('+201123456789')).toBeVisible();
  await profile.getByRole('button', { name: 'إغلاق', exact: true }).click();
  await page.goto('/staff/users');
  await page.getByRole('button', { name: 'عرض بروفايل Mona Ahmed Ali' }).click();
  await expect(page.getByRole('dialog').getByText('29901010112345')).toBeVisible();
});

test('expired invitation displays a recovery message instead of accepting', async ({ page }) => {
  const state = await install(page, 'expired');
  await page.goto(`/invite?token=${token}`);
  await expect(page.getByRole('alert').filter({ hasText: 'انتهت صلاحيتها' })).toBeVisible();
  await expect(page.getByLabel('الرقم القومي')).toHaveCount(0);
  expect(state.accepted).toBe(false);
});

test('password reset link changes only the password, not personal data', async ({ page }) => {
  const state = await install(page, 'password_reset');
  await page.goto(`/invite?token=${token}`);
  await expect(page.getByRole('heading', { name: 'إعادة تعيين كلمة المرور' })).toBeVisible();
  await expect(page.getByLabel('الرقم القومي')).toHaveCount(0);
  await page.getByLabel(/^كلمة المرور/).fill(password);
  await page.getByLabel('تأكيد كلمة المرور').fill(password);
  await page.getByRole('button', { name: 'حفظ الحساب' }).click();
  await expect(page.getByRole('heading', { name: 'تم حفظ حسابك بنجاح' })).toBeVisible();
  expect(state.accepted).toBe(true);
  expect(state.profile).toBeNull();
});
