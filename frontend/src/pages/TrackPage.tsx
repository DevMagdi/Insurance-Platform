import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError } from '../lib/api';
import { formatDate } from '../lib/status';
import { isValidNationalId, isValidTicketNo, maskNationalId } from '../lib/validation';
import { Field, StatusChip, TextInput, primaryButton } from '../components/ui';
import type { GuestTrackResult } from '../lib/types';
import { useCaptcha } from '../lib/captcha';

export function TrackForm() {
  const { t, i18n } = useTranslation();
  const [ticketNo, setTicketNo] = useState('');
  const [nationalId, setNationalId] = useState('');
  const [errors, setErrors] = useState<{ ticket?: string; national?: string }>({});
  const captcha = useCaptcha('guest_track');
  const [captchaError, setCaptchaError] = useState('');
  const track = useMutation({
    mutationFn: () => api.guestTrack(ticketNo, nationalId, captcha.token),
    onSettled: () => captcha.reset(),
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const next = {
      ticket: isValidTicketNo(ticketNo) ? undefined : 'validation.ticket',
      national: isValidNationalId(nationalId) ? undefined : 'validation.nationalId',
    };
    setErrors(next);
    if (next.ticket || next.national) return;
    if (!captcha.ready) {
      setCaptchaError(captcha.failed ? 'security.captchaFailed' : 'security.captchaRequired');
      return;
    }
    setCaptchaError('');
    track.mutate();
  };

  const result: GuestTrackResult | undefined = track.data;

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <Field label={t('track.ticketNo')} error={errors.ticket}>
        <TextInput
          value={ticketNo}
          onChange={(e) => setTicketNo(e.target.value.toUpperCase())}
          placeholder="CMP-2026-000123"
          dir="ltr"
          autoComplete="off"
        />
      </Field>
      <Field label={t('track.nationalId')} error={errors.national} hint={maskNationalId(nationalId)}>
        <TextInput
          value={nationalId}
          onChange={(e) => setNationalId(e.target.value.replace(/[^\d\s-]/g, ''))}
          inputMode="numeric"
          dir="ltr"
          autoComplete="off"
          placeholder="•••• •••• •••• ••"
        />
      </Field>
      {captcha.widget}
      {captchaError && <p role="alert" className="text-sm text-rose-500">{t(captchaError)}</p>}
      <button type="submit" className={`${primaryButton} w-full sm:w-auto`} disabled={track.isPending}>
        {t('track.action')}
      </button>
      {track.isError && (
        <p role="alert" className="rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-500">
          {track.error instanceof ApiError && track.error.status === 404
            ? t('track.notFound')
            : track.error instanceof ApiError && track.error.status === 429
              ? t('security.tooMany')
              : t('errors.generic')}
        </p>
      )}
      {result && (
        <div className="rounded-2xl border border-emerald-300/40 bg-emerald-500/10 p-4" role="status">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-mono font-bold" dir="ltr">{result.public_ticket}</span>
            <StatusChip status={result.public_status} />
          </div>
          <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-slate-500">{t('track.company')}</dt>
              <dd className="font-medium">{result.public_company}</dd>
            </div>
            <div>
              <dt className="text-slate-500">{t('track.created')}</dt>
              <dd className="font-medium">{formatDate(result.created, i18n.language)}</dd>
            </div>
          </dl>
        </div>
      )}
    </form>
  );
}

export function TrackPage() {
  const { t } = useTranslation();
  return (
    <section className="mx-auto max-w-2xl rounded-3xl border border-slate-200/80 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5">
      <h1 className="text-2xl font-bold">{t('track.title')}</h1>
      <p className="mb-5 mt-1 text-slate-500 dark:text-slate-400">{t('track.subtitle')}</p>
      <TrackForm />
    </section>
  );
}
