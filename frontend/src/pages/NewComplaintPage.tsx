import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { motion, useReducedMotion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Copy,
  FileText,
  LockKeyhole,
  Pencil,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
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
import { Button, Field, SelectInput, TextArea, TextInput, Toggle } from '../components/ui';

const STEP_KEYS = ['wizard.steps.identity', 'wizard.steps.policy', 'wizard.steps.complaint', 'wizard.steps.files', 'wizard.steps.review'] as const;
const STEP_TITLES = ['wizardExtra.identityTitle', 'wizardExtra.policyTitle', 'wizardExtra.complaintTitle', 'wizardExtra.attachmentsTitle', 'wizardExtra.reviewTitle2'] as const;
const STEP_DESCRIPTIONS = ['wizardExtra.identityDesc', 'wizardExtra.policyDesc', 'wizardExtra.complaintDesc', 'wizardExtra.attachmentsDesc', 'wizardExtra.reviewDesc'] as const;
const STEP_HINTS = ['wizardExtra.stepHint1', 'wizardExtra.stepHint2', 'wizardExtra.stepHint3', 'wizardExtra.stepHint4', 'wizardExtra.stepHint5'] as const;

interface SuccessState {
  ticketNo: string;
  complaintId: string;
  uploaded: number;
  total: number;
}

function useDirection() {
  const { i18n } = useTranslation();
  return {
    Back: i18n.language === 'ar' ? ArrowRight : ArrowLeft,
    Next: i18n.language === 'ar' ? ArrowLeft : ArrowRight,
    forward: i18n.language === 'ar' ? -1 : 1,
  };
}

export function NewComplaintPage() {
  const { t } = useTranslation();
  const { me, isAuthenticated } = useMe();
  const queryClient = useQueryClient();
  const reduced = useReducedMotion();
  const { Back, Next, forward } = useDirection();
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
  const [copied, setCopied] = useState(false);
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
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(draft.email.trim())) {
      setSubmitError(t('errors.emailInvalid'));
      return;
    }
    if (!otpCaptcha.ready) {
      setSubmitError(t(otpCaptcha.failed ? 'security.captchaFailed' : 'security.captchaRequired'));
      return;
    }
    setBusy('otp');
    setSubmitError(null);
    try {
      await api.otpStart(normalizeMobile(draft.mobile), draft.email.trim().toLowerCase(), otpCaptcha.token);
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
          email: draft.email.trim().toLowerCase(),
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

  const resetWizard = () => {
    setDraft(emptyDraft());
    setFiles([]);
    setSuccess(null);
    setStep(0);
    setConsent(false);
    setOtpCode('');
    setSubmitError(null);
  };

  if (success) {
    return (
      <div style={{ maxWidth: 600, marginInline: 'auto' }}>
        <motion.section
          className="wizard-success"
          initial={reduced ? undefined : { opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35, ease: [0.22, 0.61, 0.36, 1] }}
        >
          <span className="success-mark" aria-hidden="true">
            <CheckCircle2 size={30} strokeWidth={1.6} />
          </span>
          <h1>{t('wizard.successTitle')}</h1>
          <p>{t('wizard.successBody')}</p>
          <div className="ticket-slip">
            <span className="chip chip-gold">{t('trackExtra.ticketLabel')}</span>
            <strong dir="ltr" data-testid="ticket-number">{success.ticketNo}</strong>
            <button
              type="button"
              aria-label={t('commonExtra.copy')}
              onClick={() => {
                void navigator.clipboard?.writeText(success.ticketNo).then(() => {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1800);
                }).catch(() => undefined);
              }}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          {success.total > 0 && (
            <p className="upload-summary">
              {t('wizard.uploadSummary', { done: success.uploaded, total: success.total })}
            </p>
          )}
          <div className="wizard-success-actions">
            <Button size="lg" onClick={() => navigate(`/tickets/${success.complaintId}`)} trailingIcon={<ChevronRight size={15} />}>
              {t('wizard.viewTicket')}
            </Button>
            <Button size="lg" variant="secondary" onClick={resetWizard} leadingIcon={<RotateCcw size={14} />}>
              {t('wizard.fileAnother')}
            </Button>
          </div>
          <p style={{ marginTop: 20, fontSize: 10.5, color: 'var(--ink-400)', display: 'flex', alignItems: 'center', gap: 6 }}>
            <ShieldCheck size={13} /> {t('wizardExtra.secureNote')}
          </p>
        </motion.section>
      </div>
    );
  }

  const visibleErrors = attempted ? errors : {};

  return (
    <div className="wizard">
      <aside className="wizard-aside">
        <div className="wizard-aside-head">
          <span className="eyebrow">{t('wizard.steps.identity')}</span>
          <h1>{t('wizard.title')}</h1>
          <p>{t('hero.subtitle')}</p>
        </div>

        <div className="wizard-steps" role="list" aria-label="progress">
          {STEP_KEYS.map((key, index) => {
            const state = index === step ? 'is-active' : index < step ? 'is-done' : '';
            return (
              <button
                key={key}
                type="button"
               
                className={`wizard-step ${state}`}
                disabled={index > step}
                onClick={() => index < step && setStep(index)}
                aria-current={index === step ? 'step' : undefined}
              >
                <span className="wizard-step-dot" aria-hidden="true">
                  {index < step ? <Check size={12} strokeWidth={2.6} /> : String(index + 1).padStart(2, '0')}
                </span>
                <span className="wizard-step-label">
                  <strong>{t(key)}</strong>
                  <small>{t(STEP_HINTS[index])}</small>
                </span>
              </button>
            );
          })}
        </div>

        <div className="wizard-aside-foot">
          <span><ShieldCheck size={13} /> {t('wizardExtra.secureNote')}</span>
          <span><LockKeyhole size={13} /> {t('security.consentLabel').split('،')[0]}</span>
        </div>
      </aside>

      <div className="wizard-main">
        <div className="wizard-mobile-head">
          <div>
            <span className="eyebrow">{t('wizard.title')}</span>
            <h1>{t(STEP_TITLES[step])}</h1>
          </div>
          <div className="wizard-progress">
            <div className="wizard-progress-row">
              <strong>{t('wizardExtra.progress', { current: step + 1, total: STEP_KEYS.length })}</strong>
              <span>{t(STEP_KEYS[step])}</span>
            </div>
            <div className="wizard-progress-track">
              <div className="wizard-progress-fill" style={{ width: `${((step + 1) / STEP_KEYS.length) * 100}%` }} />
            </div>
          </div>
        </div>

        {restored && (
          <div className="restored-note" role="status">
            <Sparkles size={13} /> {t('wizard.draftRestored')}
          </div>
        )}
        {restoredNames.length > 0 && files.length === 0 && (
          <p className="field-hint" style={{ marginBottom: 10 }}>
            {t('wizard.draftFilesLost', { names: restoredNames.join(', ') })}
          </p>
        )}

        <motion.section
          key={step}
          className="wizard-card"
          initial={reduced ? undefined : { opacity: 0, x: 14 * forward }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.24, ease: [0.22, 0.61, 0.36, 1] }}
        >
          <header className="wizard-card-head">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div>
                <h2>{step === 4 ? t('wizard.review') : t(STEP_TITLES[step])}</h2>
                <p>{t(STEP_DESCRIPTIONS[step])}</p>
              </div>
              <span className="chip">{String(step + 1).padStart(2, '0')} / 05</span>
            </div>
          </header>

          <div className="wizard-card-body">
            {step === 0 && (
              <>
                <div className="form-grid-2">
                  <Field label={t('wizard.fullName')} error={visibleErrors.full_name}>
                    <TextInput
                      value={draft.full_name}
                      onChange={(e) => update({ full_name: e.target.value })}
                      autoComplete="name"
                      placeholder={t('wizard.fullName')}
                    />
                  </Field>
                  <Field
                    label={t('wizard.nationalId')}
                    error={visibleErrors.national_id}
                    hint={draft.national_id ? maskNationalId(draft.national_id) : t('wizardExtra.nationalIdHint')}
                  >
                    <TextInput
                      value={formatNationalId(draft.national_id)}
                      onChange={(e) => update({ national_id: e.target.value.replace(/\D/g, '') })}
                      inputMode="numeric"
                      dir="ltr"
                      autoComplete="off"
                      placeholder="2990 1010 1123 45"
                      className="font-mono"
                    />
                  </Field>
                </div>
                <div className="form-grid-2">
                  <Field label={t('wizard.mobile')} error={visibleErrors.mobile} hint={t('wizardExtra.mobileHint')}>
                    <TextInput
                      value={draft.mobile}
                      onChange={(e) => update({ mobile: e.target.value })}
                      inputMode="tel"
                      dir="ltr"
                      placeholder="01xxxxxxxxx"
                      autoComplete="tel"
                      className="font-mono"
                    />
                  </Field>
                  <Field label={t('wizard.email')} error={visibleErrors.email} hint={t('wizardExtra.emailHint')} optional optionalLabel={t('common.optional')}>
                    <TextInput
                      value={draft.email}
                      onChange={(e) => update({ email: e.target.value })}
                      inputMode="email"
                      dir="ltr"
                      autoComplete="email"
                    />
                  </Field>
                </div>
                <Toggle checked={draft.whatsapp_available} onChange={(v) => update({ whatsapp_available: v })} label={t('wizard.whatsapp')} />
              </>
            )}

            {step === 1 && (
              <>
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
                    <option value="">{t('wizardExtra.companyPlaceholder')}</option>
                    {companies.map((company) => (
                      <option key={company.id} value={company.id}>
                        {company.name_ar} · {company.name_en}
                      </option>
                    ))}
                  </SelectInput>
                </Field>
                {companiesQuery.isError && <p className="field-error">{t('errors.loadCompanies')}</p>}
                <div>
                  <span className="field-label" style={{ marginBottom: 7 }}>{t('wizard.type')}</span>
                  <div className="choice-grid" role="radiogroup" aria-label={t('wizard.type')}>
                    {typeOptions.map((option, index) => (
                      <button
                        key={option}
                        type="button"
                        role="radio"
                        aria-checked={draft.insurance_type === option}
                        aria-label={t(`types.${option}`)}
                        onClick={() => update({ insurance_type: option })}
                        className="choice-card"
                      >
                        <span className="choice-check" aria-hidden="true"><Check size={10} strokeWidth={3} /></span>
                        <strong>{t(`types.${option}`)}</strong>
                        <small>{String(index + 1).padStart(2, '0')}</small>
                      </button>
                    ))}
                  </div>
                  <p className="field-hint" style={{ marginTop: 6 }}>{t('wizardExtra.typeHint')}</p>
                  {visibleErrors.insurance_type && <p className="field-error" style={{ marginTop: 6 }}>{t(visibleErrors.insurance_type)}</p>}
                </div>
                <Field label={t('wizard.policyNumber')} error={visibleErrors.policy_number}>
                  <TextInput
                    value={draft.policy_number}
                    onChange={(e) => update({ policy_number: e.target.value })}
                    dir="ltr"
                    placeholder="POL-000123"
                    className="font-mono"
                  />
                </Field>
              </>
            )}

            {step === 2 && (
              <>
                <Field label={t('wizard.subject')} error={visibleErrors.subject}>
                  <TextInput
                    value={draft.subject}
                    onChange={(e) => update({ subject: e.target.value })}
                    maxLength={160}
                    placeholder={t('wizard.subject')}
                  />
                </Field>
                <Field label={t('wizard.details')} error={visibleErrors.description} hint={t('wizard.detailsHint')}>
                  <TextArea value={draft.description} onChange={(e) => update({ description: e.target.value })} maxLength={10000} />
                </Field>
                <p className="field-hint" style={{ textAlign: 'end', marginTop: -6 }}>
                  {t('wizardExtra.detailsCounter', { count: draft.description.trim().length })}
                </p>
              </>
            )}

            {step === 3 && (
              <>
                <FileUploader
                  files={files}
                  onAdd={(incoming) => setFiles((current) => [...current, ...incoming.map(toQueuedFile)])}
                  onRemove={(id) => setFiles((current) => current.filter((q) => q.id !== id))}
                />
                <p className="field-hint" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <LockKeyhole size={12} /> {t('wizardExtra.uploadWait')}
                </p>
              </>
            )}

            {step === 4 && (
              <>
                <div className="review-list">
                  <ReviewSection
                    title={t('wizardExtra.identitySection')}
                    editLabel={t('wizard.edit')}
                    onEdit={() => setStep(0)}
                    rows={[
                      [t('wizard.fullName'), draft.full_name || '—'],
                      [t('wizard.nationalId'), maskNationalId(draft.national_id)],
                      [t('wizard.mobile'), normalizeMobile(draft.mobile)],
                      [t('wizard.email'), draft.email || '—'],
                    ]}
                  />
                  <ReviewSection
                    title={t('wizardExtra.policySection')}
                    editLabel={t('wizard.edit')}
                    onEdit={() => setStep(1)}
                    rows={[
                      [t('wizard.company'), selectedCompany ? `${selectedCompany.name_ar} · ${selectedCompany.name_en}` : '—'],
                      [t('wizard.type'), draft.insurance_type ? t(`types.${draft.insurance_type}`) : '—'],
                      [t('wizard.policyNumber'), draft.policy_number || '—'],
                    ]}
                  />
                  <ReviewSection
                    title={t('wizardExtra.complaintSection')}
                    editLabel={t('wizard.edit')}
                    onEdit={() => setStep(2)}
                    rows={[[t('wizard.subject'), draft.subject || '—']]}
                    body={draft.description}
                    bodyLabel={t('wizard.details')}
                  />
                  <ReviewSection
                    title={t('wizardExtra.attachmentsSection')}
                    editLabel={t('wizard.edit')}
                    onEdit={() => setStep(3)}
                    rows={[[t('files.title'), files.length ? files.map((f) => f.file.name).join(' · ') : '—']]}
                  />
                </div>

                {!isAuthenticated && (
                  <div className="otp-panel">
                    <h3><LockKeyhole size={14} /> {t('wizard.otpTitle')}</h3>
                    <p>{t('wizard.otpHint')}</p>
                    <div className="otp-row">
                      <TextInput
                        value={otpCode}
                        onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                        inputMode="numeric"
                        dir="ltr"
                        placeholder="••••••"
                        aria-label={t('wizard.otpCode')}
                        className="font-mono"
                      />
                      <Button variant="secondary" onClick={() => void sendOtp()} loading={busy === 'otp'} style={{ flex: 'none' }}>
                        {t('wizard.sendOtp')}
                      </Button>
                    </div>
                    {otpCaptcha.widget}
                    {otpSent && <p className="field-hint" style={{ color: 'var(--emerald-600)' }}>{t('auth.codeSent')}</p>}
                  </div>
                )}

                <label className="consent-box">
                  <input
                    type="checkbox"
                    checked={consent}
                    onChange={(e) => setConsent(e.target.checked)}
                    data-testid="privacy-consent"
                  />
                  <span>{t('security.consentLabel')}</span>
                </label>

                {submitError && (
                  <p role="alert" className="alert alert-error">{submitError}</p>
                )}

                <Button
                  size="lg"
                  onClick={() => void submit()}
                  loading={busy === 'submit' || busy === 'upload'}
                  leadingIcon={<FileText size={15} />}
                  style={{ width: '100%' }}
                >
                  {busy === 'upload' ? t('wizard.uploading') : t('wizard.submitComplaint')}
                </Button>
                <p style={{ textAlign: 'center', fontSize: 10, color: 'var(--ink-400)' }}>{t('wizardExtra.summaryNotice')}</p>
              </>
            )}
          </div>

          <footer className="wizard-card-foot">
            <div className="wizard-foot-left">
              <Button
                variant="secondary"
                onClick={() => setStep((s) => Math.max(0, s - 1))}
                disabled={step === 0 || busy !== null}
                leadingIcon={<Back size={14} />}
              >
                {t('common.back')}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  clearDraft();
                  setDraft(emptyDraft());
                  setFiles([]);
                  setStep(0);
                  setErrors({});
                  setAttempted(false);
                }}
                disabled={busy !== null}
              >
                {t('common.clearDraft')}
              </Button>
            </div>
            <div className="wizard-foot-right">
              <span className="draft-note">
                <Check size={12} /> {t('common.savedDraft')}
              </span>
              {step < 4 && (
                <Button onClick={next} disabled={busy !== null} trailingIcon={<Next size={14} />}>
                  {t('common.next')}
                </Button>
              )}
            </div>
          </footer>
        </motion.section>
      </div>
    </div>
  );
}

function ReviewSection({
  title,
  editLabel,
  onEdit,
  rows,
  body,
  bodyLabel,
}: {
  title: string;
  editLabel: string;
  onEdit: () => void;
  rows: Array<[string, string]>;
  body?: string;
  bodyLabel?: string;
}) {
  return (
    <section className="review-section">
      <div className="review-section-head">
        <h3>{title}</h3>
        <button type="button" className="review-edit" onClick={onEdit}>
          <Pencil size={10} /> {editLabel}
        </button>
      </div>
      <dl className="review-rows">
        {rows.map(([label, value]) => (
          <div className="review-row" key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {body ? (
        <div className="review-body">
          <dt>{bodyLabel}</dt>
          <dd>{body}</dd>
        </div>
      ) : null}
    </section>
  );
}

