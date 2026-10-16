import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react';
import { useTranslation } from 'react-i18next';
import type { ComplaintStatus } from '../lib/types';
import { STATUS_META } from '../lib/status';

export const inputClass =
  'w-full rounded-xl border border-slate-300/60 bg-white/70 px-3.5 py-2.5 text-[15px] text-slate-900 shadow-sm outline-none transition placeholder:text-slate-400 focus:border-amber-400 focus:ring-2 focus:ring-amber-300/40 dark:border-white/15 dark:bg-white/5 dark:text-slate-50';

export const labelClass = 'mb-1.5 block text-sm font-medium text-slate-700 dark:text-slate-200';

export function FieldError({ messageKey }: { messageKey?: string }) {
  const { t } = useTranslation();
  if (!messageKey) return null;
  return (
    <p role="alert" className="mt-1.5 text-sm text-rose-500">
      {t(messageKey)}
    </p>
  );
}

export function Field({
  label,
  error,
  children,
  hint,
}: {
  label: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      {/* Nesting the control inside the label keeps the association for
          assistive tech and testing-library label queries. */}
      <label className="block">
        <span className={labelClass}>{label}</span>
        {children}
      </label>
      {hint && !error ? <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">{hint}</p> : null}
      <FieldError messageKey={error} />
    </div>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${inputClass} ${props.className ?? ''}`} />;
}

export function SelectInput(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${inputClass} ${props.className ?? ''}`} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`${inputClass} min-h-28 ${props.className ?? ''}`} />;
}

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between gap-3 rounded-xl border border-slate-300/60 bg-white/60 px-3.5 py-2.5 text-start text-[15px] dark:border-white/15 dark:bg-white/5"
    >
      <span>{label}</span>
      <span
        className={`relative h-6 w-11 shrink-0 rounded-full transition ${checked ? 'bg-emerald-500' : 'bg-slate-300 dark:bg-slate-600'}`}
      >
        <span
          className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${checked ? 'inset-inline-end-0.5 translate-x-0' : 'start-0.5'}`}
          style={checked ? { insetInlineStart: 'calc(100% - 1.375rem)' } : { insetInlineStart: '0.125rem' }}
        />
      </span>
    </button>
  );
}

export function StatusChip({ status }: { status: ComplaintStatus }) {
  const { t } = useTranslation();
  const meta = STATUS_META[status];
  return (
    <span
      data-status={status}
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ${meta.chipClass}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dotClass}`} />
      {t(meta.labelKey)}
    </span>
  );
}

export function LoadingState() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-3 py-10 text-slate-500 dark:text-slate-300" role="status">
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-amber-400 border-t-transparent" />
      {t('common.loading')}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-2xl border border-rose-300/40 bg-rose-500/10 p-4 text-rose-600 dark:text-rose-300" role="alert">
      <p>{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-2 font-semibold underline">
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300/60 p-8 text-center text-slate-500 dark:border-white/15 dark:text-slate-400">
      {message}
    </div>
  );
}

export const primaryButton =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-l from-amber-400 to-amber-500 px-5 py-2.5 font-semibold text-slate-950 shadow-lg shadow-amber-500/20 transition hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-50';

export const ghostButton =
  'inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300/70 px-4 py-2.5 font-medium transition hover:bg-slate-100 disabled:opacity-50 dark:border-white/15 dark:hover:bg-white/10';
