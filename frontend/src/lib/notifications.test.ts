import { describe, expect, it } from 'vitest';
import { complaintIdForNotice, noticeLabel } from './notifications';

describe('staff notice presentation', () => {
  it('opens the complaint chat from the row link, with the payload as fallback', () => {
    expect(complaintIdForNotice({ complaint_id: 'ticket-9', payload: { complaint_id: 'ticket-1' } })).toBe('ticket-9');
    expect(complaintIdForNotice({ complaint_id: null, payload: { complaint_id: 'ticket-1' } })).toBe('ticket-1');
    expect(complaintIdForNotice({ complaint_id: null, payload: {} })).toBeNull();
    expect(complaintIdForNotice({ complaint_id: null, payload: { complaint_id: 7 } })).toBeNull();
  });

  it('labels every kind the server can emit, in Arabic', () => {
    expect(noticeLabel('created')).toBe('طلب جديد وصلت');
    expect(noticeLabel('reply')).toBe('رسالة جديدة من عميل');
    expect(noticeLabel('assigned')).toBe('أُسندت إليك طلب');
    expect(noticeLabel('sla_breached')).toBe('تجاوز مهلة SLA على طلب');
    expect(noticeLabel('some_future_kind')).toBe('some future kind');
  });
});
