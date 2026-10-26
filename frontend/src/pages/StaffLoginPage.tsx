import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ApiError, staffApi } from '../lib/api';
import { navigate } from '../lib/router';
import { usePlatformBrand } from '../components/Branding';
import { ErrorState, Field, TextInput, primaryButton } from '../components/ui';
import { useCaptcha } from '../lib/captcha';

type Step = 'credentials' | 'password_change' | 'totp_verify' | 'sms_verify' | 'totp_setup';

export function StaffLoginPage() {
  const { t } = useTranslation();
  const brand = usePlatformBrand();
  const [step, setStep] = useState<Step>('credentials');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState('');
  const [provisioningUri, setProvisioningUri] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const captcha = useCaptcha('staff_login');

  const fail = (caught: unknown) => {
    setError(caught instanceof ApiError ? caught.message : t('errors.generic'));
  };

  const login = async () => {
    if (!captcha.ready) {
      setError(t(captcha.failed ? 'security.captchaFailed' : 'security.captchaRequired'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const result = await staffApi.login(email.trim().toLowerCase(), password, captcha.token);
      if (result.next_step === 'authenticated' || !result.challenge) {
        navigate('/staff');
        window.location.reload();
        return;
      }
      setChallenge(result.challenge);
      if (result.next_step === 'password_change') setStep('password_change');
      else if (result.next_step === 'totp_verify') setStep('totp_verify');
      else if (result.next_step === 'sms_otp') setStep('sms_verify');
      else if (result.next_step === 'totp_setup') {
        setStep('totp_setup');
        const setup = await staffApi.totpEnrollStart(result.challenge);
        setProvisioningUri(setup.provisioning_uri);
      } else {
        navigate('/staff');
      }
    } catch (caught) {
      fail(caught);
    } finally {
      captcha.reset();
      setBusy(false);
    }
  };

  const submitChallenge = async () => {
    setBusy(true);
    setError('');
    try {
      if (step === 'password_change') {
        await staffApi.changePassword(challenge, newPassword);
        setStep('credentials');
        setPassword('');
        setNewPassword('');
      } else if (step === 'totp_verify') {
        await staffApi.totpVerify(challenge, code);
        navigate('/staff');
        window.location.reload();
      } else if (step === 'sms_verify') {
        await staffApi.smsOtpVerify(challenge, code);
        navigate('/staff');
        window.location.reload();
      } else if (step === 'totp_setup') {
        await staffApi.totpEnrollConfirm(challenge, code);
        setStep('credentials');
        setPassword('');
        setCode('');
      }
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="brokerhub-login-page" dir="rtl">
      <section className="brokerhub-login-form">
        <div className="brokerhub-login-brand"><span>و</span><strong>وسيط · Broker Hub</strong></div>
        <section className="staff-auth-card">
      <span className="staff-auth-mark" aria-hidden="true">
        <img src={brand.logo_url} alt="" />
      </span>
      <p className="eyebrow">{brand.platform_name}</p>
      <h1 className="mt-2 text-3xl font-black">{t('staff.loginTitle')}</h1>
      <p className="mt-2 text-text-muted">{t('staff.loginSubtitle')}</p>
      {error && <div className="mt-4"><ErrorState message={error} /></div>}

      {step === 'credentials' ? (
        <form
          className="mt-6 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void login();
          }}
        >
          <Field label={t('staff.email')}>
            <TextInput type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          <Field label={t('staff.password')}>
            <TextInput type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </Field>
          {captcha.widget}
          <button type="submit" disabled={busy} className={`${primaryButton} w-full`}>
            {t('nav.login')}
          </button>
        </form>
      ) : (
        <form
          className="mt-6 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submitChallenge();
          }}
        >
          {step === 'password_change' && (
            <Field label={t('staff.newPassword')}>
              <TextInput type="password" autoComplete="new-password" minLength={12} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
            </Field>
          )}
          {step === 'totp_setup' && provisioningUri && (
            <div className="rounded-2xl bg-surface-elevated p-4 text-sm">
              <p className="font-semibold">{t('staff.provisioning')}</p>
              <code className="mt-2 block break-all text-xs" dir="ltr">{provisioningUri}</code>
            </div>
          )}
          {(step === 'totp_verify' || step === 'sms_verify' || step === 'totp_setup') && (
            <Field label={step === 'sms_verify' ? t('staff.smsOtp') : t('staff.totp')}>
              <TextInput inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" value={code} onChange={(e) => setCode(e.target.value)} required />
            </Field>
          )}
          <button type="submit" disabled={busy} className={`${primaryButton} w-full`}>
            {t('staff.continue')}
          </button>
        </form>
      )}
        </section>
      </section>
      <aside className="brokerhub-login-story">
        <div className="brokerhub-login-orb" aria-hidden="true" />
        <h2>كل طلبات عملائك<br />في مكان واحد</h2>
        <ul>
          <li>تابع الطلبات من واتساب والمكالمات والبريد</li>
          <li>إدارة Leads ومتابعتها حتى التحويل لعميل</li>
          <li>رؤية واضحة على أداء فريق الوساطة</li>
        </ul>
        <div className="brokerhub-sla-card"><strong>طلب #28418</strong><span>أول رد خلال ١٤ دقيقة</span><b>ضمن اتفاقية SLA</b></div>
      </aside>
    </main>
  );
}
