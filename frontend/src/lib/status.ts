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
  new: { labelKey: 'status.new', chipClass: 'status-chip status-chip--new', dotClass: 'status-dot status-dot--new' },
  assigned: { labelKey: 'status.assigned', chipClass: 'status-chip status-chip--assigned', dotClass: 'status-dot status-dot--assigned' },
  in_review: { labelKey: 'status.in_review', chipClass: 'status-chip status-chip--in_review', dotClass: 'status-dot status-dot--in_review' },
  resolved: { labelKey: 'status.resolved', chipClass: 'status-chip status-chip--resolved', dotClass: 'status-dot status-dot--resolved' },
  reopened: { labelKey: 'status.reopened', chipClass: 'status-chip status-chip--reopened', dotClass: 'status-dot status-dot--reopened' },
  transferred: { labelKey: 'status.transferred', chipClass: 'status-chip status-chip--transferred', dotClass: 'status-dot status-dot--transferred' },
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
