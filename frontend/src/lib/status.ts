import type { ComplaintStatus, TimelineEvent } from './types';

/** Canonical journey: New → Assigned → In Review → Resolved (+ Reopened / Transferred). */
export const MAIN_STEPS: ComplaintStatus[] = ['new', 'assigned', 'in_review', 'resolved'];

export interface StatusMeta {
  /** Key under `status.*` in the locale resources. */
  labelKey: string;
  chipClass: string;
  dotClass: string;
}

export const STATUS_META: Record<ComplaintStatus, StatusMeta> = {
  new: { labelKey: 'status.new', chipClass: 'bg-sky-500/15 text-sky-300 ring-sky-400/30', dotClass: 'bg-sky-400' },
  assigned: { labelKey: 'status.assigned', chipClass: 'bg-indigo-500/15 text-indigo-300 ring-indigo-400/30', dotClass: 'bg-indigo-400' },
  in_review: { labelKey: 'status.in_review', chipClass: 'bg-amber-500/15 text-amber-300 ring-amber-400/30', dotClass: 'bg-amber-400' },
  resolved: { labelKey: 'status.resolved', chipClass: 'bg-emerald-500/15 text-emerald-300 ring-emerald-400/30', dotClass: 'bg-emerald-400' },
  reopened: { labelKey: 'status.reopened', chipClass: 'bg-orange-500/15 text-orange-300 ring-orange-400/30', dotClass: 'bg-orange-400' },
  transferred: { labelKey: 'status.transferred', chipClass: 'bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-400/30', dotClass: 'bg-fuchsia-400' },
};

/**
 * Index into MAIN_STEPS for the animated timeline. Reopened returns to
 * “In Review”; Transferred is rendered as a terminal branch node instead.
 */
export function timelineStepIndex(status: ComplaintStatus): number {
  switch (status) {
    case 'new':
      return 0;
    case 'assigned':
      return 1;
    case 'in_review':
    case 'reopened':
      return 2;
    case 'resolved':
      return 3;
    case 'transferred':
      return 2;
  }
}

export function isOpenStatus(status: ComplaintStatus): boolean {
  return status !== 'resolved';
}

/** Display date in the active locale without ever showing an invalid placeholder. */
export function formatDate(value: string | null | undefined, locale: string): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(locale === 'ar' ? 'ar-EG' : 'en-GB', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function eventLabelKey(event: TimelineEvent): string {
  if (event.event_type === 'created') return 'timeline.created';
  if (event.to_status) return `status.${event.to_status}`;
  return 'timeline.updated';
}
