/**
 * Corporate client sign-in.
 *
 * A contracted company gets a username and password from the brokerage, so
 * this is deliberately separate from both the staff console and the customer
 * portal: different credentials, different session kind, different data.
 */
import { useState } from 'react';
import { ApiError } from '../../api';
import { corporateApi } from '../../lib/corporate';
import { navigate } from '../../lib/router';
import { useCaptcha } from '../../lib/captcha';

export function CorporateLoginPage() {
  const [step, setStep] = useState<'credentials' | 'password_change'>('credentials');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const captcha = useCaptcha('corporate_login');

  const fail = (caught: unknown) => {
    setError(caught instanceof ApiError ? caught.message : 'تعذر إتمام العملية. حاول مرة أخرى.');
  };

  const signIn = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await corporateApi.login(username, password, captcha.token);
      if (result.must_change_password) {
        setNotice('لا بد من تغيير كلمة المرور قبل الدخول لأول مرة.');
        setStep('password_change');
        return;
      }
      navigate('/corporate');
      window.location.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      captcha.reset();
      setBusy(false);
    }
  };

  const changePassword = async () => {
    if (newPassword !== confirmPassword) {
      setError('كلمتا المرور غير متطابقتين.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await corporateApi.changePassword(password, newPassword);
      setNotice('تم تغيير كلمة المرور. سجّل الدخول بكلمة المرور الجديدة.');
      setStep('credentials');
      setPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = username.trim().length >= 3 && password.length >= 8 && !busy;
  const canChange = newPassword.length >= 12 && confirmPassword.length >= 12 && !busy;

  return (
    <div className="corporate-auth">
      <div className="corporate-auth-card surface-card">
        <span className="corporate-auth-eyebrow">بوابة الشركات</span>
        <h1>الدخول إلى حساب شركتك</h1>
        <p className="corporate-auth-lead">
          تابع طلبات موظفيك، وثائق التأمين، وتفاصيل التعاقد مع الوسيط في مكان واحد.
        </p>

        {notice && <div className="corporate-notice" role="status">{notice}</div>}
        {error && <div className="corporate-error" role="alert">{error}</div>}

        {step === 'credentials' ? (
          <form
            className="corporate-auth-form"
            onSubmit={(event) => { event.preventDefault(); if (canSubmit) void signIn(); }}
          >
            <label className="field-label">
              اسم المستخدم
              <input
                autoFocus
                dir="ltr"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="company.hr"
              />
            </label>
            <label className="field-label">
              كلمة المرور
              <input
                type="password"
                dir="ltr"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <button className="button button-dark" type="submit" disabled={!canSubmit}>
              {busy ? 'جارٍ الدخول…' : 'تسجيل الدخول'}
            </button>
          </form>
        ) : (
          <form
            className="corporate-auth-form"
            onSubmit={(event) => { event.preventDefault(); if (canChange) void changePassword(); }}
          >
            <label className="field-label">
              كلمة المرور الجديدة
              <input
                autoFocus
                type="password"
                dir="ltr"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
              <span className="field-hint">12 حرفاً على الأقل.</span>
            </label>
            <label className="field-label">
              تأكيد كلمة المرور
              <input
                type="password"
                dir="ltr"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </label>
            <button className="button button-dark" type="submit" disabled={!canChange}>
              {busy ? 'جارٍ الحفظ…' : 'حفظ كلمة المرور'}
            </button>
          </form>
        )}

        <p className="corporate-auth-foot">
          هذه البوابة مخصصة للشركات المتعاقدة. للموظفين الأفراد، استخدم
          {' '}<a href="/login">بوابة العملاء</a>.
        </p>
      </div>
    </div>
  );
}
