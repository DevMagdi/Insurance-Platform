import type { NotificationItem } from '../types';

/**
 * Notice presentation helpers shared by the staff console.
 *
 * The console bell is the entry point to a complaint's chat: a customer
 * message fans a scoped notification out on the server, and clicking the
 * notice must open that exact ticket. The link therefore resolves from the
 * notification row first (always present for ticket notices) and from the
 * payload second (older rows), so no notice kind can open a wrong chat.
 */

const NOTICE_LABELS: Record<string, string> = {
  created: 'طلب جديد وصلت',
  reply: 'رسالة جديدة من عميل',
  reopened: 'أُعيد فتح طلب',
  status_changed: 'تغيّرت حالة طلب',
  assigned: 'أُسندت إليك طلب',
  complaint_assigned: 'أُسندت إليك طلب',
  sla_breached: 'تجاوز مهلة SLA على طلب',
  access_changed: 'تم تحديث صلاحياتك',
  team_membership_changed: 'تم تحديث عضويتك في فريق',
};

export function noticeLabel(kind: string): string {
  return NOTICE_LABELS[kind] ?? kind.replaceAll('_', ' ');
}

export function complaintIdForNotice(
  notice: Pick<NotificationItem, 'complaint_id' | 'payload'>,
): string | null {
  if (typeof notice.complaint_id === 'string' && notice.complaint_id) return notice.complaint_id;
  const fromPayload = notice.payload?.complaint_id;
  return typeof fromPayload === 'string' && fromPayload ? fromPayload : null;
}
