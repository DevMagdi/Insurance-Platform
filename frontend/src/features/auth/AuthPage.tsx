import { useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';

interface AuthPageProps {
  onAuthenticated: () => Promise<void> | void;
}

type AuthMode = 'login' | 'totp' | 'password-change' | 'totp-setup' | 'invite';

interface StaffLoginResult {
  next_step: string | null;
  challenge: string | null;
}

export function AuthPage({ onAuthenticated }: AuthPageProps) {
  const inviteToken = new URLSearchParams(window.location.search).get('token');
  const [mode, setMode] = useState<AuthMode>(inviteToken ? 'invite' : 'login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState('');
  const [provisioningUri, setProvisioningUri] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر إكمال العملية. حاول مرة أخرى.');
    } finally {
      setBusy(false);
    }
  };

  const signIn = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void run(async () => {
      const result = await api<StaffLoginResult>('/api/auth/staff/login', {
        method: 'POST',
        body: jsonBody({ email, password }),
      });
      setChallenge(result.challenge ?? '');
      if (result.next_step === 'totp_verify') {
        setMode('totp');
        return;
      }
      if (result.next_step === 'password_change') {
        setMode('password-change');
        return;
      }
      if (result.next_step === 'totp_setup') {
        const setup = await api<{ provisioning_uri: string }>('/api/auth/staff/2fa/enroll/start', {
          method: 'POST',
          body: jsonBody({ challenge: result.challenge }),
        });
        setProvisioningUri(setup.provisioning_uri);
        setMode('totp-setup');
        return;
      }
      await onAuthenticated();
    });
  };

  const verifyTotp = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void run(async () => {
      await api('/api/auth/staff/2fa/verify', {
        method: 'POST',
        body: jsonBody({ challenge, code }),
      });
      await onAuthenticated();
    });
  };

  const changePassword = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void run(async () => {
      await api('/api/auth/staff/password/change', {
        method: 'POST',
        body: jsonBody({ challenge, password: newPassword }),
      });
      setPassword('');
      setNewPassword('');
      setMode('login');
      setNotice('تم تحديث كلمة المرور. سجّل الدخول بكلمة المرور الجديدة.');
    });
  };

  const confirmTotpSetup = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void run(async () => {
      await api('/api/auth/staff/2fa/enroll/confirm', {
        method: 'POST',
        body: jsonBody({ challenge, code }),
      });
      setMode('login');
      setPassword('');
      setNotice('تم تفعيل المصادقة الثنائية. سجّل الدخول لإكمال العملية.');
    });
  };

  const acceptInvite = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void run(async () => {
      await api('/api/auth/staff/invitation/accept', {
        method: 'POST',
        body: jsonBody({ token: inviteToken, password: newPassword }),
      });
      window.history.replaceState({}, '', window.location.pathname);
      setMode('login');
      setNewPassword('');
      setNotice('تم إعداد الحساب. يمكنك تسجيل الدخول الآن.');
    });
  };

  const modeTitle: Record<AuthMode, string> = {
    login: 'مرحباً بعودتك',
    totp: 'تحقق من هويتك',
    'password-change': 'اختر كلمة مرور جديدة',
    'totp-setup': 'فعّل المصادقة الثنائية',
    invite: 'انضم إلى مساحة وَثيق',
  };

  return (
    <main className="auth-page" dir="rtl">
      <div className="auth-atmosphere auth-atmosphere-one" />
      <div className="auth-atmosphere auth-atmosphere-two" />
      <div className="auth-layout">
        <section className="auth-story">
          <div className="brand-mark auth-brand-mark">و</div>
          <p className="auth-kicker">WASIQ · INSURANCE OPERATIONS</p>
          <h1>كل شكوى،<br /><span>بمسار أوضح.</span></h1>
          <p className="auth-story-copy">
            مساحة آمنة لفريق الوساطة لمتابعة شكاوى التأمين، وقياس جودة الخدمة، وإدارة الوصول بدقة.
          </p>
          <div className="auth-trust-row">
            <span><Icon name="shield" size={16} /> صلاحيات ونطاقات محمية</span>
            <span><Icon name="chart" size={16} /> رؤية تشغيلية لحظية</span>
          </div>
          <div className="auth-orbit" aria-hidden="true"><span /><span /><span /></div>
        </section>

        <section className="auth-card">
          <div className="auth-card-top">
            <div className="auth-card-logo"><span className="brand-mark">و</span><strong>وَثيق</strong></div>
            <span className="auth-secure"><Icon name="lock" size={14} /> اتصال آمن</span>
          </div>
          <p className="eyebrow">مساحة فريق الوساطة</p>
          <h2>{modeTitle[mode]}</h2>
          <p className="auth-subtitle">
            {mode === 'login'
              ? 'سجّل الدخول للوصول إلى لوحة العمليات.'
              : mode === 'totp'
                ? 'أدخل الرمز المكوّن من ٦ أرقام من تطبيق المصادقة.'
                : mode === 'password-change'
                  ? 'يلزم تحديث كلمة المرور قبل فتح مساحة العمل.'
                  : mode === 'totp-setup'
                    ? 'أضف هذا الرابط إلى تطبيق المصادقة ثم أدخل الرمز.'
                    : 'أنشئ كلمة مرور آمنة لإكمال الدعوة.'}
          </p>

          {mode === 'login' && (
            <form className="auth-form" onSubmit={signIn}>
              <label className="field-label">البريد الإلكتروني
                <input autoComplete="username" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@company.com" dir="ltr" />
              </label>
              <label className="field-label">كلمة المرور
                <input autoComplete="current-password" type="password" required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="••••••••••••" dir="ltr" />
              </label>
              <button className="button button-gold auth-submit" disabled={busy} type="submit">
                {busy ? 'جارٍ التحقق…' : 'تسجيل الدخول'} <Icon name="arrow" size={16} />
              </button>
            </form>
          )}

          {mode === 'totp' && (
            <form className="auth-form" onSubmit={verifyTotp}>
              <label className="field-label">رمز المصادقة
                <input autoFocus autoComplete="one-time-code" inputMode="numeric" maxLength={6} required value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))} placeholder="000000" dir="ltr" />
              </label>
              <button className="button button-gold auth-submit" disabled={busy || code.length !== 6} type="submit">تحقق ومتابعة</button>
            </form>
          )}

          {mode === 'password-change' && (
            <form className="auth-form" onSubmit={changePassword}>
              <label className="field-label">كلمة المرور الجديدة
                <input autoFocus autoComplete="new-password" type="password" minLength={12} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} placeholder="١٢ حرفاً على الأقل" dir="ltr" />
              </label>
              <button className="button button-gold auth-submit" disabled={busy} type="submit">حفظ كلمة المرور</button>
            </form>
          )}

          {mode === 'totp-setup' && (
            <form className="auth-form" onSubmit={confirmTotpSetup}>
              <div className="provisioning-box">
                <span>رابط الإعداد</span><code dir="ltr">{provisioningUri}</code>
              </div>
              <label className="field-label">رمز التحقق
                <input autoFocus inputMode="numeric" maxLength={6} required value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))} placeholder="000000" dir="ltr" />
              </label>
              <button className="button button-gold auth-submit" disabled={busy || code.length !== 6} type="submit">تأكيد التفعيل</button>
            </form>
          )}

          {mode === 'invite' && (
            <form className="auth-form" onSubmit={acceptInvite}>
              <label className="field-label">كلمة المرور
                <input autoComplete="new-password" type="password" minLength={12} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} placeholder="١٢ حرفاً على الأقل" dir="ltr" />
              </label>
              <button className="button button-gold auth-submit" disabled={busy} type="submit">تفعيل الحساب</button>
            </form>
          )}

          {error && <p className="form-alert error" role="alert">{error}</p>}
          {notice && <p className="form-alert success" role="status">{notice}</p>}
          <div className="auth-footer"><Icon name="shield" size={15} /> يتم تطبيق صلاحياتك الحالية على كل طلب بيانات.</div>
        </section>
      </div>
      <p className="auth-copyright">© 2026 Wāsiq · منصة تشغيل وساطة التأمين</p>
    </main>
  );
}
