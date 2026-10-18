import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';
import { InvitePage } from './InvitePage';

vi.mock('../api', () => ({ api: vi.fn(), jsonBody: JSON.stringify }));
vi.mock('../lib/captcha', () => ({ useCaptcha: () => ({ ready: true, failed: false, token: 'captcha-proof', reset: vi.fn(), widget: null }), captchaHeaders: (token: string) => ({ 'X-Captcha-Token': token }) }));
const token = 't'.repeat(43);
const invitation = { email: 'employee@gmail.com', full_name: 'Mona Ali', department_name: 'التعويضات', kind: 'invitation' };

beforeEach(() => { window.history.replaceState({}, '', `/invite#token=${token}`); vi.mocked(api).mockResolvedValue(invitation); });
afterEach(() => { vi.clearAllMocks(); window.history.replaceState({}, '', '/'); });

function setPassword(confirmation = 'Cedar-River-Garden-49') {
  fireEvent.change(screen.getByLabelText(/^كلمة المرور/), { target: { value: 'Cedar-River-Garden-49' } });
  fireEvent.change(screen.getByLabelText('تأكيد كلمة المرور'), { target: { value: confirmation } });
}

async function fillProfile() {
  await screen.findByLabelText('الاسم الكامل');
  fireEvent.change(screen.getByLabelText('الاسم الكامل'), { target: { value: 'Mona Ahmed Ali' } });
  fireEvent.change(screen.getByLabelText('الرقم القومي'), { target: { value: '29901010112345' } });
  fireEvent.change(screen.getByLabelText('رقم الهاتف'), { target: { value: '+201123456789' } });
  fireEvent.change(screen.getByLabelText('العنوان'), { target: { value: '12 Nile Street, Dokki' } });
  fireEvent.change(screen.getByLabelText('المدينة / المحافظة'), { target: { value: 'Giza' } });
  setPassword();
}

describe('invitation onboarding', () => {
  it('opens the email link, removes its token from history and submits personal details plus photo', async () => {
    render(<InvitePage />);
    await fillProfile();
    expect(window.location.hash).toBe('');
    expect(vi.mocked(api).mock.calls[0]).toEqual(['/api/auth/staff/invitation/inspect', expect.objectContaining({ body: JSON.stringify({ token }) })]);
    expect(screen.getByText('employee@gmail.com')).toBeTruthy();
    const file = new File(['test photo bytes'], 'portrait.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText(/^الصورة الشخصية/), { target: { files: [file] } });
    await screen.findByAltText('معاينة الصورة الشخصية');
    fireEvent.submit(screen.getByRole('button', { name: 'حفظ الحساب' }).closest('form')!);
    await screen.findByText('تم حفظ حسابك بنجاح');
    const [, request] = vi.mocked(api).mock.calls.find(([path]) => path === '/api/auth/staff/invitation/accept')!;
    const body = JSON.parse(String(request?.body));
    expect(body.token).toBe(token);
    expect(body.profile).toEqual({ full_name: 'Mona Ahmed Ali', national_id: '29901010112345', mobile: '+201123456789', address: '12 Nile Street, Dokki', city: 'Giza', job_title: '', emergency_phone: null });
    expect(body.avatar_base64).toBe(btoa('test photo bytes'));
    expect(body.profile.department_id).toBeUndefined();
    expect(body.profile.email).toBeUndefined();
    expect(request?.headers).toEqual({ 'X-Captcha-Token': 'captcha-proof' });
    expect(screen.getByRole('link', { name: 'تسجيل الدخول' }).getAttribute('href')).toBe('/staff/login');
  });

  it('requires a photo and refuses oversized or unsafe files without sending acceptance', async () => {
    render(<InvitePage />);
    await fillProfile();
    fireEvent.submit(screen.getByRole('button', { name: 'حفظ الحساب' }).closest('form')!);
    expect(await screen.findByText('الصورة الشخصية مطلوبة.')).toBeTruthy();
    const file = new File(['<svg/>'], 'attack.svg', { type: 'image/svg+xml' });
    fireEvent.change(screen.getByLabelText(/^الصورة الشخصية/), { target: { files: [file] } });
    expect(await screen.findByText(/لا تتجاوز 512/)).toBeTruthy();
    const oversized = new File([new Uint8Array(512 * 1024 + 1)], 'large.jpg', { type: 'image/jpeg' });
    fireEvent.change(screen.getByLabelText(/^الصورة الشخصية/), { target: { files: [oversized] } });
    expect(screen.queryByAltText('معاينة الصورة الشخصية')).toBeNull();
    expect(vi.mocked(api).mock.calls.filter(([path]) => path.endsWith('/accept'))).toHaveLength(0);
  });

  it('handles legacy query-string links and resets a password without overwriting the profile', async () => {
    window.history.replaceState({}, '', `/invite?token=${token}`);
    vi.mocked(api).mockResolvedValue({ ...invitation, kind: 'password_reset' });
    render(<InvitePage />);
    await screen.findByText('إعادة تعيين كلمة المرور');
    expect(window.location.search).toBe('');
    expect(screen.queryByLabelText('الرقم القومي')).toBeNull();
    setPassword('different-pass-48');
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الحساب' }));
    expect(await screen.findByText('كلمتا المرور غير متطابقتين.')).toBeTruthy();
    setPassword();
    fireEvent.click(screen.getByRole('button', { name: 'حفظ الحساب' }));
    await screen.findByText('تم حفظ حسابك بنجاح');
    const [, request] = vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/accept'))!;
    expect(JSON.parse(String(request?.body))).toEqual({ token, password: 'Cedar-River-Garden-49' });
  });

  it('shows an actionable expired-link error and never displays the profile form', async () => {
    vi.mocked(api).mockRejectedValue(new Error('expired'));
    render(<InvitePage />);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByLabelText('الاسم الكامل')).toBeNull();
  });

  it('does not inspect an incomplete link', async () => {
    window.history.replaceState({}, '', '/invite');
    render(<InvitePage />);
    expect(screen.getByRole('alert').textContent).toContain('غير مكتمل');
    await waitFor(() => expect(api).not.toHaveBeenCalled());
  });
});
