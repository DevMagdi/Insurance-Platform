import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import { ApiError, api, uploadFiles } from '../lib/api';
import { useCaptcha } from '../lib/captcha';
import {
  clearDraft,
  emptyDraft,
  loadDraft,
  loadDraftFileNames,
  saveDraft,
  saveDraftFileNames,
  type ComplaintDraft,
} from '../lib/draft';
import { toQueuedFile, type QueuedFile } from '../lib/files';
import {
  complaintStepSchema,
  formatNationalId,
  identityStepSchema,
  isValidNationalId,
  isValidMobile,
  maskNationalId,
  normalizeMobile,
  policyStepSchema,
  zodFieldErrors,
  type FieldErrorMap,
} from '../lib/validation';
import { navigate } from '../lib/router';
import { useMe } from '../hooks/useMe';
import { FileUploader } from '../components/FileUploader';
import { Field, SelectInput, TextArea, TextInput, Toggle, ghostButton, primaryButton } from '../components/ui';

const STEP_KEYS = ['wizard.steps.identity', 'wizard.steps.policy', 'wizard.steps.complaint', 'wizard.steps.files', 'wizard.steps.review'] as const;

interface SuccessState {
  ticketNo: string;
  complaintId: string;
  uploaded: number;
  total: number;
}

export function NewComplaintPage() {
  const { t } = useTranslation();
  const { me, isAuthenticated } = useMe();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ComplaintDraft>(() => loadDraft() ?? emptyDraft());
  const [step, setStep] = useState(() => Math.min(loadDraft()?.step ?? 0, 4));
  const [errors, setErrors] = useState<FieldErrorMap>({});
  const [attempted, setAttempted] = useState(false);
  const [files, setFiles] = useState<QueuedFile[]>([]);
  const [restoredNames] = useState<string[]>(() => loadDraftFileNames());
  const [restored] = useState(() => {
    const existing = loadDraft();
    return Boolean(existing && (existing.full_name || existing.subject || existing.policy_number));
  });
  const [otpCode, setOtpCode] = useState('');
  const [otpSent, setOtpSent] = useState(false);
  const [busy, setBusy] = useState<'otp' | 'submit' | 'upload' | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [success, setSuccess] = useState<SuccessState | null>(null);
  // PDPL: explicit, purpose-bound consent is recorded server-side before submission.
  const [consent, setConsent] = useState(false);
  const otpCaptcha = useCaptcha('otp_start');

  const companiesQuery = useQuery({ queryKey: ['companies'], queryFn: api.companies, staleTime: 300_000 });
  const companies = useMemo(() => companiesQuery.data ?? [], [companiesQuery.data]);
  const selectedCompany = companies.find((c) => c.id === draft.company_id);
  const typeOptions = selectedCompany?.insurance_types ?? (['health', 'general', 'both'] as const);

  // Prefill from the signed-in profile when there is nothing typed yet
  // (React's adjust-state-during-render pattern, guarded per account).
  const [prefilledFor, setPrefilledFor] = useState<string | null>(null);
  if (me && prefilledFor !== me.id && !draft.full_name && !draft.mobile) {
    setPrefilledFor(me.id);
    setDraft((d) => ({ ...d, full_name: me.full_name, mobile: me.mobile, email: me.email ?? '' }));
  }

  // Autosave (text only) whenever the draft or step changes.
  useEffect(() => {
    if (success) return;
    const timer = setTimeout(() => {
      saveDraft({ ...draft, step });
      saveDraftFileNames(files.map((f) => f.file.name));
    }, 350);
    return () => clearTimeout(timer);
  }, [draft, step, files, success]);

  const update = (patch: Partial<ComplaintDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const validateStep = (index: number): FieldErrorMap => {
    if (index === 0) {
      const result = identityStepSchema.safeParse({
        full_name: draft.full_name,
        national_id: draft.national_id,
        mobile: draft.mobile,
        whatsapp_available: draft.whatsapp_available,
        email: draft.email,
      });
      const map = result.success ? {} : zodFieldErrors(result.error);
      // Live, explicit checks give the same messages with clearer precedence.
      if (!isValidMobile(draft.mobile)) map.mobile = 'validation.mobile';
      if (!isValidNationalId(draft.national_id)) map.national_id = 'validation.nationalId';
      if (draft.full_name.trim().length < 2) map.full_name = 'validation.fullName';
      return map;
    }
    if (index === 1) {
      const result = policyStepSchema.safeParse({
        company_id: draft.company_id,
        insurance_type: draft.insurance_type || 'health',
        policy_number: draft.policy_number,
      });
      const map = result.success ? {} : zodFieldErrors(result.error);
      if (!draft.company_id) map.company_id = 'validation.company';
      if (!draft.insurance_type) map.insurance_type = 'validation.insuranceType';
      return map;
    }
    if (index === 2) {
      const result = complaintStepSchema.safeParse({ subject: draft.subject, description: draft.description });
      return result.success ? {} : zodFieldErrors(result.error);
    }
    return {};
  };

  const next = () => {
    const map = validateStep(step);
    setErrors(map);
    setAttempted(true);
    if (Object.keys(map).length === 0) {
      setAttempted(false);
      setStep((s) => Math.min(s + 1, 4));
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const sendOtp = async () => {
    if (!otpCaptcha.ready) {
      setSubmitError(t(otpCaptcha.failed ? 'security.captchaFailed' : 'security.captchaRequired'));
      return;
    }
    setBusy('otp');
    setSubmitError(null);
    try {
      await api.otpStart(normalizeMobile(draft.mobile), otpCaptcha.token);
      setOtpSent(true);
    } catch (error) {
      setSubmitError(error instanceof ApiError && error.status === 429 ? t('security.tooMany') : t('errors.generic'));
    } finally {
      otpCaptcha.reset();
      setBusy(null);
    }
  };

  const uploadQueued = async (complaintId: string, queue: QueuedFile[]) => {
    const pending = queue.filter((q) => q.status !== 'done');
    if (pending.length === 0) return { done: queue.filter((q) => q.status === 'done').length, total: queue.length };
    setBusy('upload');
    setFiles((current) =>
      current.map((q) => (pending.some((p) => p.id === q.id) ? { ...q, status: 'uploading', progress: 0 } : q)),
    );
    const results = await uploadFiles(
      complaintId,
      pending.map((q) => q.file),
      (index, percent) => {
        const target = pending[index];
        setFiles((current) => current.map((q) => (q.id === target.id ? { ...q, progress: percent } : q)));
      },
    );
    let doneCount = queue.filter((q) => q.status === 'done').length;
    results.forEach((result, index) => {
      const target = pending[index];
      if (result.ok) doneCount += 1;
      setFiles((current) =>
        current.map((q) =>
          q.id === target.id
            ? { ...q, status: result.ok ? 'done' : 'error', progress: result.ok ? 100 : q.progress, error: result.error }
            : q,
        ),
      );
    });
    setBusy(null);
    return { done: doneCount, total: queue.length };
  };

  const submit = async () => {
    const identityErrors = { ...validateStep(0), ...validateStep(1), ...validateStep(2) };
    if (Object.keys(identityErrors).length > 0) {
      setErrors(identityErrors);
      setStep(0);
      return;
    }
    if (!consent) {
      setSubmitError(t('security.consentRequired'));
      return;
    }
    setBusy('submit');
    setSubmitError(null);
    try {
      if (!isAuthenticated) {
        if (!/^[0-9]{6}$/.test(otpCode)) {
          setSubmitError(t('validation.otp'));
          setBusy(null);
          return;
        }
        await api.otpVerify({
          mobile: normalizeMobile(draft.mobile),
          code: otpCode,
          full_name: draft.full_name.trim(),
          national_id: draft.national_id.replace(/[\s-]/g, ''),
          whatsapp_available: draft.whatsapp_available,
          email: draft.email.trim() || undefined,
        });
        await queryClient.invalidateQueries({ queryKey: ['me'] });
      }
      await api.recordConsent('complaint_processing', true);
      const created = await api.createComplaint(
        {
          national_id: draft.national_id.replace(/[\s-]/g, ''),
          mobile: normalizeMobile(draft.mobile),
          company_id: draft.company_id,
          insurance_type: draft.insurance_type as 'health' | 'general' | 'both',
          policy_number: draft.policy_number.trim(),
          subject: draft.subject.trim(),
          description: draft.description.trim(),
        },
        draft.idempotency_key,
      );
      const uploadOutcome = files.length > 0 ? await uploadQueued(created.id, files) : { done: 0, total: 0 };
      clearDraft();
      void queryClient.invalidateQueries({ queryKey: ['my-complaints'] });
      void queryClient.invalidateQueries({ queryKey: ['my-counters'] });
      setSuccess({
        ticketNo: created.ticket_no,
        complaintId: created.id,
        uploaded: uploadOutcome.done,
        total: uploadOutcome.total,
      });
    } catch (error) {
      setSubmitError(error instanceof ApiError && error.status === 429 ? t('security.tooMany') : t('errors.generic'));
    } finally {
      setBusy(null);
    }
  };

  if (success) {
    return (
      <section className="mx-auto max-w-xl rounded-3xl border border-emerald-300/40 bg-white/70 p-8 text-center shadow-sm backdrop-blur dark:bg-white/5">
        <motion.div initial={{ scale: 0.7, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-emerald-500/15 text-3xl">
          ✓
        </motion.div>
        <h1 className="text-2xl font-bold">{t('wizard.successTitle')}</h1>
        <p className="mt-2 text-slate-500 dark:text-slate-300">{t('wizard.successBody')}</p>
        <p className="my-5 rounded-2xl bg-slate-900 px-4 py-3 font-mono text-xl font-bold tracking-wider text-amber-300 dark:bg-black/40" dir="ltr" data-testid="ticket-number">
          {success.ticketNo}
        </p>
        {success.total > 0 && (
          <p className="mb-4 text-sm text-slate-500 dark:text-slate-300">
            {t('wizard.uploadSummary', { done: success.uploaded, total: success.total })}
          </p>
        )}
        <div className="flex flex-wrap justify-center gap-3">
          <button type="button" className={primaryButton} onClick={() => navigate(`/tickets/${success.complaintId}`)}>
            {t('wizard.viewTicket')}
          </button>
          <button
            type="button"
            className={ghostButton}
            onClick={() => {
              setDraft(emptyDraft());
              setFiles([]);
              setSuccess(null);
              setStep(0);
            }}
          >
            {t('wizard.fileAnother')}
          </button>
        </div>
      </section>
    );
  }

  const visibleErrors = attempted ? errors : {};

  return (
    <section className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-bold">{t('wizard.title')}</h1>
      <p className="mt-1 text-sm text-emerald-600 dark:text-emerald-300" role="status">{t('common.savedDraft')}</p>
      {restored && (
        <p className="mt-2 rounded-xl bg-amber-400/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-300">{t('wizard.draftRestored')}</p>
      )}
      {restoredNames.length > 0 && files.length === 0 && (
        <p className="mt-2 text-sm text-slate-500">{t('wizard.draftFilesLost', { names: restoredNames.join(', ') })}</p>
      )}

      <ol className="mt-6 flex gap-1.5" aria-label="progress">
        {STEP_KEYS.map((key, index) => (
          <li key={key} className="flex-1">
            <button
              type="button"
              disabled={index > step}
              onClick={() => index < step && setStep(index)}
              className={`w-full rounded-full px-1 py-1.5 text-[11px] font-semibold transition sm:text-xs ${
                index === step
                  ? 'bg-gradient-to-l from-amber-400 to-amber-500 text-slate-950'
                  : index < step
                    ? 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-300'
                    : 'bg-slate-200/80 text-slate-500 dark:bg-white/10'
              }`}
            >
              {t(key)}
            </button>
          </li>
        ))}
      </ol>

      <motion.div
        key={step}
        initial={{ opacity: 0, x: 16 }}
        animate={{ opacity: 1, x: 0 }}
        className="mt-5 rounded-3xl border border-slate-200/80 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5"
      >
        {step === 0 && (
          <div className="space-y-4">
            <Field label={t('wizard.fullName')} error={visibleErrors.full_name}>
              <TextInput value={draft.full_name} onChange={(e) => update({ full_name: e.target.value })} autoComplete="name" />
            </Field>
            <Field label={t('wizard.nationalId')} error={visibleErrors.national_id} hint={draft.national_id ? maskNationalId(draft.national_id) : t('wizard.nationalIdHint')}>
              <TextInput
                value={formatNationalId(draft.national_id)}
                onChange={(e) => update({ national_id: e.target.value.replace(/\D/g, '') })}
                inputMode="numeric"
                dir="ltr"
                autoComplete="off"
                placeholder="2990 1010 1123 45"
              />
            </Field>
            <Field label={t('wizard.mobile')} error={visibleErrors.mobile}>
              <TextInput value={draft.mobile} onChange={(e) => update({ mobile: e.target.value })} inputMode="tel" dir="ltr" placeholder="01xxxxxxxxx" autoComplete="tel" />
            </Field>
            <Toggle checked={draft.whatsapp_available} onChange={(v) => update({ whatsapp_available: v })} label={t('wizard.whatsapp')} />
            <Field label={`${t('wizard.email')} (${t('common.optional')})`} error={visibleErrors.email}>
              <TextInput value={draft.email} onChange={(e) => update({ email: e.target.value })} inputMode="email" dir="ltr" autoComplete="email" />
            </Field>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-4">
            <Field label={t('wizard.company')} error={visibleErrors.company_id}>
              <SelectInput
                value={draft.company_id}
                onChange={(e) => {
                  const company = companies.find((c) => c.id === e.target.value);
                  update({
                    company_id: e.target.value,
                    insurance_type: company && draft.insurance_type && !company.insurance_types.includes(draft.insurance_type as 'health')
                      ? ''
                      : draft.insurance_type,
                  });
                }}
              >
                <option value="">—</option>
                {companies.map((company) => (
                  <option key={company.id} value={company.id}>
                    {company.name_ar} · {company.name_en}
                  </option>
                ))}
              </SelectInput>
            </Field>
            {companiesQuery.isError && <p className="text-sm text-rose-500">{t('errors.loadCompanies')}</p>}
            <div>
              <span className="mb-2 block text-sm font-medium">{t('wizard.type')}</span>
              <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label={t('wizard.type')}>
                {typeOptions.map((option) => (
                  <button
                    key={option}
                    type="button"
                    role="radio"
                    aria-checked={draft.insurance_type === option}
                    onClick={() => update({ insurance_type: option })}
                    className={`rounded-xl border px-3 py-2.5 text-sm font-medium transition ${
                      draft.insurance_type === option
                        ? 'border-amber-400 bg-amber-400/15 text-amber-600 dark:text-amber-300'
                        : 'border-slate-300/60 dark:border-white/15'
                    }`}
                  >
                    {t(`types.${option}`)}
                  </button>
                ))}
              </div>
              {visibleErrors.insurance_type && <p className="mt-1.5 text-sm text-rose-500">{t(visibleErrors.insurance_type)}</p>}
            </div>
            <Field label={t('wizard.policyNumber')} error={visibleErrors.policy_number}>
              <TextInput value={draft.policy_number} onChange={(e) => update({ policy_number: e.target.value })} dir="ltr" placeholder="POL-000123" />
            </Field>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <Field label={t('wizard.subject')} error={visibleErrors.subject}>
              <TextInput value={draft.subject} onChange={(e) => update({ subject: e.target.value })} maxLength={160} />
            </Field>
            <Field label={t('wizard.details')} error={visibleErrors.description} hint={t('wizard.detailsHint')}>
              <TextArea value={draft.description} onChange={(e) => update({ description: e.target.value })} maxLength={10000} />
            </Field>
            <p className="text-end text-xs text-slate-400">{draft.description.trim().length} / 20+</p>
          </div>
        )}

        {step === 3 && (
          <FileUploader
            files={files}
            onAdd={(incoming) => setFiles((current) => [...current, ...incoming.map(toQueuedFile)])}
            onRemove={(id) => setFiles((current) => current.filter((q) => q.id !== id))}
          />
        )}

        {step === 4 && (
          <div className="space-y-5">
            <h2 className="font-semibold">{t('wizard.review')}</h2>
            <dl className="space-y-2.5 text-sm">
              {[
                [t('wizard.fullName'), draft.full_name, 0],
                [t('wizard.nationalId'), maskNationalId(draft.national_id), 0],
                [t('wizard.mobile'), normalizeMobile(draft.mobile), 0],
                [t('wizard.company'), selectedCompany ? `${selectedCompany.name_ar} · ${selectedCompany.name_en}` : '—', 1],
                [t('wizard.type'), draft.insurance_type ? t(`types.${draft.insurance_type}`) : '—', 1],
                [t('wizard.policyNumber'), draft.policy_number, 1],
                [t('wizard.subject'), draft.subject, 2],
                [t('files.title'), files.length ? files.map((f) => f.file.name).join(', ') : '—', 3],
              ].map(([label, value, target]) => (
                <div key={String(label)} className="flex items-start justify-between gap-3 rounded-xl bg-slate-900/[0.03] px-3 py-2 dark:bg-white/5">
                  <div className="min-w-0">
                    <dt className="text-slate-500">{label}</dt>
                    <dd className="truncate font-medium">{String(value)}</dd>
                  </div>
                  <button type="button" className="shrink-0 text-sm font-semibold text-amber-600 dark:text-amber-300" onClick={() => setStep(Number(target))}>
                    {t('wizard.edit')}
                  </button>
                </div>
              ))}
            </dl>
            <p className="rounded-xl bg-slate-900/[0.03] px-3 py-2 text-sm leading-6 dark:bg-white/5">{draft.description}</p>

            {!isAuthenticated && (
              <div className="space-y-3 rounded-2xl border border-amber-300/40 bg-amber-400/5 p-4">
                <h3 className="font-semibold">{t('wizard.otpTitle')}</h3>
                <p className="text-sm text-slate-500 dark:text-slate-300">{t('wizard.otpHint')}</p>
                <div className="flex gap-2">
                  <TextInput
                    value={otpCode}
                    onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    inputMode="numeric"
                    dir="ltr"
                    placeholder="••••••"
                    aria-label={t('wizard.otpCode')}
                  />
                  <button type="button" className={ghostButton} onClick={() => void sendOtp()} disabled={busy === 'otp'}>
                    {t('wizard.sendOtp')}
                  </button>
                </div>
                {otpCaptcha.widget}
                {otpSent && <p className="text-sm text-emerald-500">{t('auth.codeSent')}</p>}
              </div>
            )}
            <label className="flex items-start gap-3 rounded-xl border border-slate-200/80 px-3 py-3 text-sm leading-6 dark:border-white/10">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 shrink-0 accent-amber-500"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                data-testid="privacy-consent"
              />
              <span>{t('security.consentLabel')}</span>
            </label>
            {submitError && (
              <p role="alert" className="rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-500">{submitError}</p>
            )}
            <button type="button" className={`${primaryButton} w-full`} onClick={() => void submit()} disabled={busy !== null}>
              {busy === 'upload' ? t('wizard.uploading') : t('wizard.submitComplaint')}
            </button>
          </div>
        )}

        <div className="mt-6 flex items-center justify-between gap-3">
          <button type="button" className={ghostButton} disabled={step === 0 || busy !== null} onClick={() => setStep((s) => Math.max(0, s - 1))}>
            {t('common.back')}
          </button>
          <button
            type="button"
            className={ghostButton}
            onClick={() => {
              clearDraft();
              setDraft(emptyDraft());
              setFiles([]);
              setStep(0);
            }}
          >
            {t('common.clearDraft')}
          </button>
          {step < 4 && (
            <button type="button" className={primaryButton} onClick={next}>
              {t('common.next')}
            </button>
          )}
        </div>
      </motion.div>
    </section>
  );
}
