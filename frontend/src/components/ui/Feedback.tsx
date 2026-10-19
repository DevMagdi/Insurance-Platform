import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Inbox, RefreshCw } from 'lucide-react';
import { cn } from '../../lib/utils';

export function LoadingState({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <div className="loading-block" role="status" aria-live="polite">
      <div className="loading-skeleton" aria-hidden="true">
        <Skeleton className="loading-skeleton-title" />
        <Skeleton />
        <Skeleton className="loading-skeleton-short" />
      </div>
      <span>{label ?? t('common.loading')}</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="alert alert-error" role="alert">
      <AlertTriangle size={15} aria-hidden="true" />
      <span>{message}</span>
      {onRetry && (
        <button type="button" onClick={onRetry} className="btn btn-sm btn-secondary" style={{ marginInlineStart: 'auto' }}>
          <RefreshCw size={12} aria-hidden="true" />
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  message,
  title,
  detail,
  icon,
  action,
  className,
}: {
  message?: string;
  title?: string;
  detail?: string;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('empty-state', className)}>
      <span className="empty-state-icon" aria-hidden="true">
        {icon ?? <Inbox size={20} />}
      </span>
      <strong>{title ?? message ?? '—'}</strong>
      {detail ? <p>{detail}</p> : null}
      {action ? <div style={{ marginTop: 8 }}>{action}</div> : null}
    </div>
  );
}

export function Skeleton({ className = 'h-4 w-full' }: { className?: string }) {
  return <div aria-hidden="true" className={cn('skeleton', className)} />;
}

export function Spinner({ size = 'md' }: { size?: 'sm' | 'md' | 'lg' }) {
  return <span className={cn('spinner', size === 'lg' && 'spinner-lg')} role="status" aria-label="loading" />;
}
