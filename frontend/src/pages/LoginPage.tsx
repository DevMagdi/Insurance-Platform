import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ArrowRight, KeyRound, LockKeyhole, ShieldCheck, Smartphone, UserRound } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { navigate } from '../lib/router';
import { isValidNationalId, isValidMobile, normalizeMobile } from '../lib/validation';
import { Button, Field, TextInput, Toggle } from '../components/ui';
import { useCaptcha } from '../lib/captcha';

/** Mobile OTP sign-in; first-time customers register with name + national ID. */
export function LoginPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [mobile, setMobile] = useState('');
  const [code, setCode] = useState('');
  const [fullName, setFullName] = useState('');
  const [nationalId, setNationalId] = useState('');
  const [whatsapp, setWhatsapp] = useState(false);
  const [needsRegister, setNeedsRegister] = useState(false);
  const [registrationPrompt, setRegistrationPrompt] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const Back = i18n.language === 'ar' ? ArrowRight : ArrowLeft;

  const captcha = useCaptcha('otp_start');
  const start = useMutation({
    mutationFn: () => api.otpStart(normalizeMobile(mobile), captcha.token),
    onSettled: () => captcha.reset(),
  });

  const verify = useMutation({
    onMutate: () => {
      if (needsRegister) setRegistrationPrompt(false);
    },
    mutationFn: () =>
      api.otpVerify({
        mobile: normalizeMobile(mobile),
        code,
        ...(needsRegister
          ? {
              full_name: fullName.trim(),
              national_id: nationalId.replace(/[\s-]/g, ''),
              whatsapp_available: whatsapp,
            }
          : {}),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['me'] });
      navigate('/tickets');
    },
    onError: (error) => {
      // The first 409 asks for identity; later conflicts must remain visible.
      if (error instanceof ApiError && error.status === 409 && !needsRegister) {
        setNeedsRegister(true);
        setRegistrationPrompt(true);
      }
    },
  });

  const sendCode = () => {
    if (!isValidMobile(mobile)) {
      setErrors({ mobile: 'validation.mobile' });
      return;
    }
    if (!captcha.ready) {
      setErrors({ mobile: captcha.failed ? 'security.captchaFailed' : 'security.captchaRequired' });
      return;
    }
    setErrors({});
    start.mutate();
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const next: Record<string, string> = {};
    if (!isValidMobile(mobile)) next.mobile = 'validation.mobile';
    if (!/^[0-9]{6}$/.test(code)) next.code = 'validation.otp';
    if (needsRegister) {
      if (fullName.trim().length < 2) next.fullName = 'validation.fullName';
      if (!isValidNationalId(nationalId)) next.nationalId = 'validation.nationalId';
    }
    setErrors(next);
    if (Object.keys(next).length === 0) verify.mutate();
  };

  const step = needsRegister ? 3 : start.isSuccess ? 2 : 1;

  return (
    <div className="auth-wrap">
      <div className="auth-card">
        <div className="auth-card-head">
          <span className="eyebrow"><LockKeyhole size={11} /> {t('home.trust1')}</span>
          <h1>{t('auth.title')}</h1>
          <p className="auth-lead">{t('auth.subtitle')}</p>
        </div>

        <div className="auth-steps" aria-hidden="true">
          {[Smartphone, KeyRound, UserRound].map((Icon, index) => (
            <span key={index} className={`auth-step${step > index ? ' is-done' : ''}${step === index + 1 ? ' is-active' : ''}`}>
              <Icon size={12} />
              <span>0{index + 1}</span>
            </span>
          ))}
        </div>

        <form onSubmit={submit} noValidate>
          <Field label={t('wizard.mobile')} error={errors.mobile}>
            <div className="auth-mobile-row">
              <TextInput
                value={mobile}
                onChange={(e) => {
                  setMobile(e.target.value);
                  setCode('');
                  start.reset();
                  verify.reset();
                  setErrors({});
                  setNeedsRegister(false);
                  setRegistrationPrompt(false);
                  setFullName('');
                  setNationalId('');
                }}
                inputMode="tel"
                dir="ltr"
                placeholder="01xxxxxxxxx"
                autoComplete="tel"
              />
              <Button type="button" variant="secondary" onClick={sendCode} loading={start.isPending} style={{ flex: 'none' }}>
                {t('auth.sendCode')}
              </Button>
            </div>
          </Field>

          {captcha.widget}

          {start.isError && (
            <p role="alert" className="alert alert-error">
              {start.error instanceof ApiError && start.error.status === 429 ? t('security.tooMany') : t('errors.generic')}
            </p>
          )}
          {start.isSuccess && (
            <p role="status" className="alert alert-success">{t('auth.codeSent')}</p>
          )}

          <Field label={t('auth.code')} error={errors.code}>
            <TextInput
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              inputMode="numeric"
              dir="ltr"
              placeholder="••••••"
              autoComplete="one-time-code"
              style={{ letterSpacing: '0.45em', fontFamily: 'var(--font-mono)' }}
            />
          </Field>

          {needsRegister && (
            <>
              <p className="alert alert-info">{t('auth.needRegister')}</p>
              <Field label={t('wizard.fullName')} error={errors.fullName}>
                <TextInput value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" />
              </Field>
              <Field label={t('wizard.nationalId')} error={errors.nationalId}>
                <TextInput
                  value={nationalId}
                  onChange={(e) => setNationalId(e.target.value.replace(/[^\d\s-]/g, ''))}
                  inputMode="numeric"
                  dir="ltr"
                  autoComplete="off"
                  placeholder="2990 1010 1123 45"
                  style={{ fontFamily: 'var(--font-mono)' }}
                />
              </Field>
              <Toggle checked={whatsapp} onChange={setWhatsapp} label={t('wizard.whatsapp')} />
            </>
          )}

          {verify.isError && !registrationPrompt && (
            <p role="alert" className="alert alert-error">{t('errors.generic')}</p>
          )}

          <Button type="submit" size="lg" loading={verify.isPending} trailingIcon={<Back size={15} />}>
            {t('auth.verify')}
          </Button>
        </form>

        <p className="auth-trust">
          <ShieldCheck size={12} /> {t('trackExtra.securityNote')}
        </p>
        <p className="auth-alt">
          <button type="button" onClick={() => navigate('/track')}>{t('track.title')}</button>
          <span>·</span>
          <button type="button" onClick={() => navigate('/complaints/new')}>{t('hero.ctaFile')}</button>
        </p>
      </div>
    </div>
  );
}
