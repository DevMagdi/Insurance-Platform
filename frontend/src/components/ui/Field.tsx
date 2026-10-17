import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle } from 'lucide-react';
import { cn } from '../../lib/utils';

export const inputClass =
  'input';
export const labelClass = 'field-label';

export function FieldError({ messageKey }: { messageKey?: string }) {
  const { t } = useTranslation();
  if (!messageKey) return null;
  const copy = messageKey.includes('.') ? t(messageKey) : messageKey;
  if (!copy) return null;
  return (
    <p role="alert" className="field-error">
      <AlertCircle size={12} aria-hidden="true" />
      <span>{copy}</span>
    </p>
  );
}

export interface FieldProps {
  label: string;
  error?: string;
  hint?: string;
  optional?: boolean;
  optionalLabel?: string;
  children: ReactNode;
  className?: string;
  htmlFor?: string;
}

/**
 * Label + control + hint/error. The control stays nested in the label so
 * assistive technology and label queries keep a single, reliable association.
 */
export function Field({ label, error, hint, optional, optionalLabel, children, className, htmlFor }: FieldProps) {
  return (
    <div className={cn('field', className)}>
      <label className="block" htmlFor={htmlFor}>
        <span className="field-label">
          <span>{label}</span>
          {optional && <span className="field-optional">{optionalLabel}</span>}
        </span>
        {children}
      </label>
      {hint && !error ? <p className="field-hint">{hint}</p> : null}
      <FieldError messageKey={error} />
    </div>
  );
}

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function TextInput(
  { className, ...props },
  ref,
) {
  return <input ref={ref} {...props} className={cn('input', className)} />;
});

export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function TextArea(
  { className, ...props },
  ref,
) {
  return <textarea ref={ref} {...props} className={cn('textarea', className)} />;
});

export const SelectInput = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function SelectInput(
  { className, ...props },
  ref,
) {
  return <select ref={ref} {...props} className={cn('select', className)} />;
});

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
      className="switch"
    >
      <span>{label}</span>
      <span className="switch-track" aria-hidden="true">
        <span className="switch-thumb" />
      </span>
    </button>
  );
}

/** Inline search field with a leading icon. */
export function SearchInput({
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <div className={cn('input-wrap', className)}>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-4-4" strokeLinecap="round" />
      </svg>
      <input id={id} {...props} className="input" />
    </div>
  );
}
