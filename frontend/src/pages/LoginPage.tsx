import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError } from '../lib/api';
import { navigate } from '../lib/router';
import { isValidNationalId, isValidMobile, normalizeMobile } from '../lib/validation';
import { Field, TextInput, Toggle, primaryButton } from '../components/ui';
import { useCaptcha } from '../lib/captcha';

/** Mobile OTP sign-in; first-time customers register with name + national ID. */
export function LoginPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [mobile, setMobile] = useState('');
  const [code, setCode] = useState('');
  const [fullName, setFullName] = useState('');
  const [nationalId, setNationalId] = useState('');
  const [whatsapp, setWhatsapp] = useState(false);
  const [needsRegister, setNeedsRegister] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const captcha = useCaptcha('otp_start');
  const start = useMutation({
    mutationFn: () => api.otpStart(normalizeMobile(mobile), captcha.token),
    onSettled: () => captcha.reset(),
  });

  const verify = useMutation({
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
      // A 409 from verify means this mobile is new: collect registration identity.
      if (error instanceof ApiError && error.status === 409) setNeedsRegister(true);
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

  return (
    <section className="mx-auto max-w-md rounded-3xl border border-slate-200/80 bg-white/70 p-6 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5">
      <h1 className="text-2xl font-bold">{t('auth.title')}</h1>
      <p className="mb-5 mt-1 text-sm text-slate-500 dark:text-slate-400">{t('auth.subtitle')}</p>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label={t('wizard.mobile')} error={errors.mobile}>
          <div className="flex gap-2">
            <TextInput
              value={mobile}
              onChange={(e) => setMobile(e.target.value)}
              inputMode="tel"
              dir="ltr"
              placeholder="01xxxxxxxxx"
              autoComplete="tel"
            />
            <button type="button" className={primaryButton} onClick={sendCode} disabled={start.isPending}>
              {t('auth.sendCode')}
            </button>
          </div>
        </Field>
        {captcha.widget}
        {start.isError && (
          <p role="alert" className="text-sm text-rose-500">
            {start.error instanceof ApiError && start.error.status === 429 ? t('security.tooMany') : t('errors.generic')}
          </p>
        )}
        {start.isSuccess && (
          <p role="status" className="rounded-xl bg-emerald-500/10 px-3 py-2 text-sm text-emerald-600 dark:text-emerald-300">
            {t('auth.codeSent')}
          </p>
        )}
        <Field label={t('auth.code')} error={errors.code}>
          <TextInput
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            inputMode="numeric"
            dir="ltr"
            placeholder="••••••"
            autoComplete="one-time-code"
          />
        </Field>
        {needsRegister && (
          <>
            <p className="text-sm text-amber-600 dark:text-amber-300">{t('auth.needRegister')}</p>
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
              />
            </Field>
            <Toggle checked={whatsapp} onChange={setWhatsapp} label={t('wizard.whatsapp')} />
          </>
        )}
        {verify.isError && !needsRegister && (
          <p role="alert" className="text-sm text-rose-500">{t('errors.generic')}</p>
        )}
        <button type="submit" className={`${primaryButton} w-full`} disabled={verify.isPending}>
          {t('auth.verify')}
        </button>
      </form>
    </section>
  );
}
