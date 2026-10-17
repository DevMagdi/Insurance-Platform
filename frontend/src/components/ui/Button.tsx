import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cn } from '../../lib/utils';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'destructive' | 'danger' | 'soft' | 'gold' | 'light';
export type ButtonSize = 'sm' | 'md' | 'lg' | 'xl' | 'icon';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
  children?: ReactNode;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  destructive: 'btn-destructive',
  danger: 'btn-destructive',
  soft: 'btn-soft',
  gold: 'btn-gold',
  light: 'btn-light',
};

const SIZES: Record<ButtonSize, string> = {
  sm: 'btn-sm',
  md: '',
  lg: 'btn-lg',
  xl: 'btn-xl',
  icon: 'btn-icon',
};

/**
 * Design-system button. Variants share the same geometry and focus ring;
 * loading keeps the label's width stable while a spinner replaces the content.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', loading = false, className, children, leadingIcon, trailingIcon, disabled, type = 'button', ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn('btn', VARIANTS[variant], SIZES[size], loading && 'is-loading', className)}
      {...props}
    >
      {leadingIcon}
      {children}
      {trailingIcon}
      {loading && (
        <span className="btn-spinner" aria-hidden="true">
          <span className="spinner" />
        </span>
      )}
    </button>
  );
});
