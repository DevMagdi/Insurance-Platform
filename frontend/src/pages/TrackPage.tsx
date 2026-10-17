import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ArrowRight, CheckCircle2, Clock3, HelpCircle, LockKeyhole, Search, ShieldCheck } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { formatDate } from '../lib/status';
import { isValidNationalId, isValidTicketNo, maskNationalId } from '../lib/validation';
import { Button, Field, StatusChip, TextInput } from '../components/ui';
import type { GuestTrackResult } from '../lib/types';
import { useCaptcha } from '../lib/captcha';
import { navigate } from '../lib/router';

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
  const errorMessage = track.isError
    ? track.error instanceof ApiError && track.error.status === 404
      ? t('track.notFound')
      : track.error instanceof ApiError && track.error.status === 429
        ? t('security.tooMany')
        : t('errors.generic')
    : '';

  return (
    <form onSubmit={submit} className="stack" style={{ gap: 15 }} noValidate>
      <Field label={t('track.ticketNo')} error={errors.ticket} hint="CMP-YYYY-NNNNNN">
        <TextInput
          value={ticketNo}
          onChange={(e) => setTicketNo(e.target.value.toUpperCase())}
          placeholder="CMP-2026-000123"
          dir="ltr"
          autoComplete="off"
          className="font-mono"
          aria-invalid={Boolean(errors.ticket) || undefined}
        />
      </Field>
      <Field label={t('track.nationalId')} error={errors.national} hint={nationalId ? maskNationalId(nationalId) : undefined}>
        <TextInput
          value={nationalId}
          onChange={(e) => setNationalId(e.target.value.replace(/[^\d\s-]/g, ''))}
          inputMode="numeric"
          dir="ltr"
          autoComplete="off"
          placeholder="•••• •••• •••• ••"
          className="font-mono"
          aria-invalid={Boolean(errors.national) || undefined}
        />
      </Field>
      {captcha.widget}
      {captchaError && <p role="alert" className="field-error">{t(captchaError)}</p>}
      <Button type="submit" size="lg" loading={track.isPending} leadingIcon={<Search size={15} />} style={{ width: '100%' }}>
        {t('track.action')}
      </Button>
      {track.isError && (
        <div role="alert" className="alert alert-error">
          {errorMessage}
        </div>
      )}
      {result && (
        <div className="track-result" role="status">
          <div className="track-result-head">
            <div className="row" style={{ gap: 8 }}>
              <span style={{ display: 'grid', placeItems: 'center', width: 26, height: 26, borderRadius: 8, background: 'var(--surface-1)', color: 'var(--emerald-600)' }}>
                <CheckCircle2 size={15} />
              </span>
              <div>
                <strong style={{ display: 'block', fontFamily: 'inherit', fontSize: 11.5 }}>{t('trackExtra.resultTitle')}</strong>
                <span className="font-mono" dir="ltr" style={{ fontSize: 10.5, color: 'var(--ink-400)' }}>{result.public_ticket}</span>
              </div>
            </div>
            <StatusChip status={result.public_status} />
          </div>
          <dl className="track-result-body">
            <div>
              <dt>{t('trackExtra.statusLabel')}</dt>
              <dd>{result.public_status ? t(`status.${result.public_status}`) : '—'}</dd>
            </div>
            <div>
              <dt>{t('trackExtra.companyLabel')}</dt>
              <dd>{result.public_company}</dd>
            </div>
            <div>
              <dt>{t('trackExtra.createdLabel')}</dt>
              <dd>{formatDate(result.created, i18n.language)}</dd>
            </div>
            <div>
              <dt>{t('trackExtra.ticketLabel')}</dt>
              <dd className="font-mono" dir="ltr">{result.public_ticket}</dd>
            </div>
          </dl>
        </div>
      )}
    </form>
  );
}

export function TrackPage() {
  const { t } = useTranslation();
  const Arrow = useLeftArrow();

  return (
    <div>
      <div className="page-intro">
        <div>
          <span className="eyebrow">{t('home.trackEyebrow')}</span>
          <h1>{t('track.title')}</h1>
          <p>{t('track.subtitle')}</p>
        </div>
        <div className="page-intro-actions">
          <Button variant="secondary" size="sm" onClick={() => navigate('/tickets')} trailingIcon={<Arrow size={14} />}>
            {t('nav.tickets')}
          </Button>
        </div>
      </div>

      <div className="track-layout">
        <section className="track-form-card card" aria-labelledby="track-form-title">
          <header>
            <h2 id="track-form-title">{t('trackExtra.formTitle')}</h2>
            <p>{t('trackExtra.formDesc')}</p>
          </header>
          <TrackForm />
        </section>

        <aside className="track-help">
          <section className="track-help-card card">
            <h3><HelpCircle size={15} /> {t('trackExtra.helpTitle')}</h3>
            <ol>
              {[t('trackExtra.helpStep1'), t('trackExtra.helpStep2'), t('trackExtra.helpStep3')].map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </section>
          <div className="track-security-note">
            <ShieldCheck size={16} />
            <span>{t('trackExtra.securityNote')}</span>
          </div>
          <section className="track-help-card card">
            <h3><Clock3 size={15} /> {t('footer.support')}</h3>
            <dl className="side-list" style={{ marginTop: 8 }}>
              <div className="side-row" style={{ paddingInline: 0 }}>
                <dt>{t('footer.hours')}</dt>
                <dd dir="ltr">9:00–18:00</dd>
              </div>
              <div className="side-row" style={{ paddingInline: 0 }}>
                <dt>{t('footer.encrypted')}</dt>
                <dd><LockKeyhole size={13} aria-hidden="true" /></dd>
              </div>
            </dl>
          </section>
        </aside>
      </div>
    </div>
  );
}

function useLeftArrow() {
  const { i18n } = useTranslation();
  return i18n.language === 'ar' ? ArrowLeft : ArrowRight;
}
