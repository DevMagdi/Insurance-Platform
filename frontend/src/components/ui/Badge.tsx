import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { ComplaintStatus } from '../../lib/types';
import { STATUS_META } from '../../lib/status';
import { cn } from '../../lib/utils';

export function StatusChip({ status }: { status: ComplaintStatus }) {
  const { t } = useTranslation();
  const meta = STATUS_META[status];
  return (
    <span data-status={status} className={cn('status-chip', `status-chip--${status}`)}>
      <span className={cn('status-dot', meta.dotClass)} aria-hidden="true" />
      {t(meta.labelKey)}
    </span>
  );
}

export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'gold' | 'emerald' | 'info' | 'warning' | 'danger';
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: 'chip',
    gold: 'chip chip-gold',
    emerald: 'chip chip-emerald',
    info: 'chip chip-info',
    warning: 'chip chip-warning',
    danger: 'chip chip-danger',
  };
  return <span className={cn(tones[tone], className)}>{children}</span>;
}

type Priority = 'low' | 'normal' | 'high' | 'urgent';

const PRIORITY_LABEL: Record<Priority, string> = {
  low: 'low',
  normal: 'normal',
  high: 'high',
  urgent: 'urgent',
};

export function PriorityChip({ priority, label }: { priority: Priority; label: string }) {
  return <span className={cn('priority', `priority-${PRIORITY_LABEL[priority]}`)}>{label}</span>;
}
