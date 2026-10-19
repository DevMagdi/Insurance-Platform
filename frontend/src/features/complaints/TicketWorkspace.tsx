import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { api, apiResponse, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { formatDateTime, formatNumber } from '../../utils/date';
import type {
  AgentOption,
  Attachment,
  AuditEvent,
  Complaint,
  ComplaintFilterOptions,
  ComplaintStatus,
  StaffAccess,
  TicketTimeline,
} from '../../types';

const STATUS_LABEL: Record<ComplaintStatus, string> = {
  new: 'جديدة', assigned: 'مُسندة', in_review: 'قيد المراجعة', resolved: 'تم الحل', reopened: 'أُعيد فتحها', transferred: 'محوّلة',
};
const CHANNEL_LABEL: Record<string, string> = { portal: 'بوابة العملاء', email: 'البريد الإلكتروني', whatsapp: 'واتساب', phone: 'مكالمة', ai_voice: 'مكالمة صوتية ذكية' };

interface TicketWorkspaceProps {
  id: string;
  access: StaffAccess;
  options: ComplaintFilterOptions;
  onBack: () => void;
  onOpenTicket: (id: string) => void;
  onRefreshAccess: () => Promise<void>;
}

interface CannedReply {
  id: string;
  title: string;
  body_en: string;
  body_ar: string;
}
interface DuplicateCandidate {
  id: string;
  ticket_no: string;
  status: ComplaintStatus;
  subject: string;
  created_at: string;
}
interface AttachmentUploadReservation {
  id: string;
  upload_url: string;
  upload_token: string;
  size_bytes: number;
}

function StatusPill({ status }: { status: ComplaintStatus }) {
  return <span className={`status-pill status-${status}`}><i />{STATUS_LABEL[status]}</span>;
}

export function TicketWorkspace({ id, access, options, onBack, onOpenTicket, onRefreshAccess }: TicketWorkspaceProps) {
  const [ticket, setTicket] = useState<Complaint | null>(null);
  const [timeline, setTimeline] = useState<TicketTimeline | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [cannedReplies, setCannedReplies] = useState<CannedReply[]>([]);
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [replyMode, setReplyMode] = useState<'public' | 'internal'>('public');
  const [reply, setReply] = useState('');
  const [sending, setSending] = useState(false);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const [newStatus, setNewStatus] = useState<ComplaintStatus>('in_review');
  const [selectedAgent, setSelectedAgent] = useState('');
  const [reason, setReason] = useState('');
  const [showAssign, setShowAssign] = useState(false);
  const [showTransfer, setShowTransfer] = useState(false);
  const [showDuplicates, setShowDuplicates] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async (silent = false) => {
    if (!hasPermission(access, 'complaints.view')) {
      setLoading(false);
      return;
    }
    if (!silent) {
      setLoading(true);
      setError('');
    }
    try {
      const detail = await api<Complaint>(`/api/staff/complaints/${encodeURIComponent(id)}`);
      setTicket(detail);
      setNewStatus(detail.status === 'new' || detail.status === 'reopened' ? 'assigned' : 'in_review');
      const requests: Array<Promise<unknown>> = [
        api<TicketTimeline>(`/api/staff/complaints/${encodeURIComponent(id)}/timeline`),
      ];
      if (hasPermission(access, 'complaints.view_attachments')) requests.push(api<Attachment[]>(`/api/staff/complaints/${encodeURIComponent(id)}/attachments`));
      if (hasPermission(access, 'complaints.reply_public')) requests.push(api<CannedReply[]>(`/api/staff/complaints/${encodeURIComponent(id)}/canned-replies`));
      if (hasPermission(access, 'complaints.view')) requests.push(api<DuplicateCandidate[]>(`/api/staff/complaints/${encodeURIComponent(id)}/duplicates`));
      if (hasPermission(access, 'audit.view')) requests.push(api<AuditEvent[]>(`/api/staff/audit/events?complaint_id=${encodeURIComponent(id)}&limit=100`));
      const resolved = await Promise.allSettled(requests);
      if (resolved[0]?.status === 'fulfilled') setTimeline(resolved[0].value as TicketTimeline);
      let cursor = 1;
      if (hasPermission(access, 'complaints.view_attachments')) {
        const item = resolved[cursor++];
        if (item?.status === 'fulfilled') setAttachments(item.value as Attachment[]);
      } else setAttachments([]);
      if (hasPermission(access, 'complaints.reply_public')) {
        const item = resolved[cursor++];
        if (item?.status === 'fulfilled') setCannedReplies(item.value as CannedReply[]);
      } else setCannedReplies([]);
      if (hasPermission(access, 'complaints.view')) {
        const item = resolved[cursor++];
        if (item?.status === 'fulfilled') setDuplicates(item.value as DuplicateCandidate[]);
      }
      if (hasPermission(access, 'audit.view')) {
        const item = resolved[cursor++];
        if (item?.status === 'fulfilled') setAudit(item.value as AuditEvent[]);
      } else setAudit([]);
    } catch (cause) {
      if (!silent) {
        setTicket(null);
        setError(cause instanceof Error ? cause.message : 'هذه الشكوى غير متاحة ضمن نطاق صلاحيتك.');
      }
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) {
        await onRefreshAccess();
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [access, id, onRefreshAccess]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void refresh(); });
    return () => { active = false; };
  }, [refresh]);

  const refreshRef = useRef(refresh);
  useEffect(() => { refreshRef.current = refresh; }, [refresh]);

  // Live ticket: SSE pushes a new signature when the customer replies (or
  // status/attachments change); a quiet poll covers SSE outages. Neither
  // path needs a manual page refresh.
  useEffect(() => {
    if (!hasPermission(access, 'complaints.view')) return;
    let lastSignature = '';
    const onEvent = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as { signature?: string };
        if (payload.signature && payload.signature !== lastSignature) {
          lastSignature = payload.signature;
          void refreshRef.current(true);
          // Something changed on this ticket (customer reply, status,
          // attachment): keep the console's notification count in step.
          window.dispatchEvent(new Event('staff:data-changed'));
        }
      } catch { /* Heartbeats and malformed frames need no action. */ }
    };
    let source: EventSource | null = null;
    try {
      source = new EventSource(`/api/staff/complaints/${encodeURIComponent(id)}/stream`);
      source.addEventListener('snapshot', onEvent as EventListener);
      source.addEventListener('update', onEvent as EventListener);
    } catch { source = null; }
    const fallback = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshRef.current(true);
    }, 15_000);
    return () => {
      source?.close();
      window.clearInterval(fallback);
    };
  }, [access, id]);

  const companyName = ticket ? options.companies.find((company) => company.id === ticket.company_id)?.name_ar
    ?? options.companies.find((company) => company.id === ticket.company_id)?.name_en
    ?? ticket.company_name
    ?? 'شركة التأمين' : '';
  const currentAgent = ticket ? options.agents.find((agent) => agent.id === ticket.assigned_agent_id)?.full_name : undefined;
  const canAssign = hasPermission(access, ticket?.assigned_agent_id ? 'complaints.reassign' : 'complaints.assign');

  const sendMessage = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ticket || !reply.trim()) return;
    const permission = replyMode === 'public' ? 'complaints.reply_public' : 'complaints.add_internal_note';
    if (!hasPermission(access, permission)) return;
    setSending(true);
    setError('');
    try {
      await api(`/api/staff/complaints/${encodeURIComponent(id)}/${replyMode === 'public' ? 'reply' : 'notes'}`, {
        method: 'POST', body: jsonBody({ body: reply.trim() }),
      });
      setReply('');
      setNotice(replyMode === 'public' ? 'تم إرسال الرد إلى سجل الشكوى.' : 'تمت إضافة الملاحظة الداخلية.' );
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر إرسال الرسالة.');
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) await onRefreshAccess();
    } finally {
      setSending(false);
    }
  };

  const uploadAttachments = async (selectedFiles: FileList | File[]) => {
    if (!ticket || !hasPermission(access, 'complaints.view_attachments')) return;
    const files = Array.from(selectedFiles);
    if (files.length === 0) return;
    if (files.length > 5) {
      setError('يمكن رفع خمسة ملفات كحد أقصى في المرة الواحدة.');
      return;
    }
    if (files.some((file) => !['application/pdf', 'image/jpeg', 'image/png'].includes(file.type))) {
      setError('الأنواع المسموحة هي PDF وJPEG وPNG فقط.');
      return;
    }
    if (files.some((file) => file.size < 1 || file.size > 10_485_760) || files.reduce((total, file) => total + file.size, 0) > 25_000_000) {
      setError('الحد الأقصى 10 ميغابايت للملف و25 ميغابايت إجمالاً.');
      return;
    }

    setUploadingAttachments(true);
    setError('');
    let completed = 0;
    try {
      const metadata = await Promise.all(files.map(async (file) => {
        const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        const checksum_sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
        return { filename: file.name, mime_type: file.type, size_bytes: file.size, checksum_sha256 };
      }));
      const plan = await api<{ uploads: AttachmentUploadReservation[] }>(`/api/staff/complaints/${encodeURIComponent(id)}/attachments/plan`, {
        method: 'POST', body: jsonBody({ files: metadata }),
      });
      if (plan.uploads.length !== files.length) throw new Error('تعذر إنشاء مسارات آمنة لجميع الملفات.');
      for (const [index, reservation] of plan.uploads.entries()) {
        const file = files[index];
        if (!file || reservation.size_bytes !== file.size) throw new Error('تغيّر حجم الملف بعد إنشاء مسار الرفع.');
        await apiResponse(reservation.upload_url, {
          method: 'PUT',
          headers: { 'X-Upload-Token': reservation.upload_token, 'Content-Type': file.type },
          body: file,
        });
        completed += 1;
      }
      setNotice(`تم رفع ${formatNumber(completed)} ملف. تظهر حالة الفحص وقابلية الفتح في قائمة المرفقات.`);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر رفع المرفقات.');
      if (completed > 0) await refresh();
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) await onRefreshAccess();
    } finally {
      setUploadingAttachments(false);
    }
  };

  const updateStatus = async () => {
    setActionBusy(true);
    setError('');
    try {
      await api(`/api/staff/complaints/${encodeURIComponent(id)}/status`, {
        method: 'PATCH', body: jsonBody({ status: newStatus }),
      });
      setNotice(`تم تحديث الحالة إلى «${STATUS_LABEL[newStatus]}».`);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تغيير الحالة.');
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) await onRefreshAccess();
    } finally { setActionBusy(false); }
  };

  const assign = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedAgent) return;
    setActionBusy(true);
    try {
      const isReassignment = Boolean(ticket?.assigned_agent_id);
      await api(`/api/staff/complaints/${encodeURIComponent(id)}/${isReassignment ? 'reassign' : 'assign'}`, {
        method: 'POST', body: jsonBody({ agent_id: selectedAgent, team_id: null }),
      });
      setShowAssign(false);
      setNotice('تم تحديث الإسناد بنجاح.');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر إسناد الشكوى.');
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) await onRefreshAccess();
    } finally { setActionBusy(false); }
  };

  const transfer = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (reason.trim().length < 10) return;
    setActionBusy(true);
    try {
      const result = await api<{ status: ComplaintStatus; delivery: string }>(`/api/staff/complaints/${encodeURIComponent(id)}/escalate`, {
        method: 'POST', body: jsonBody({ reason: reason.trim() }),
      });
      setShowTransfer(false);
      setReason('');
      setNotice(result.delivery === 'queued' ? 'تم تحويل الشكوى إلى الشركة ووضع البريد في قائمة الإرسال.' : 'تم تحويل الشكوى.');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تحويل الشكوى.');
      if (cause && typeof cause === 'object' && 'status' in cause && (cause as { status: number }).status === 403) await onRefreshAccess();
    } finally { setActionBusy(false); }
  };

  const mergeInto = async (targetId: string) => {
    setActionBusy(true);
    try {
      await api(`/api/staff/complaints/${encodeURIComponent(id)}/merge`, { method: 'POST', body: jsonBody({ target_id: targetId }) });
      setShowDuplicates(false);
      setNotice('تم دمج الشكوى.');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر دمج الشكويين.');
    } finally { setActionBusy(false); }
  };

  const downloadAttachment = async (attachment: Attachment) => {
    if (attachment.scan_status !== 'clean') return;
    try {
      const authorization = await api<{ download_url: string; download_token: string }>(`/api/staff/attachments/${encodeURIComponent(attachment.id)}/authorize`);
      const response = await apiResponse(authorization.download_url, { headers: { 'X-Download-Token': authorization.download_token } });
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      if (attachment.mime_type === 'application/pdf' || attachment.mime_type.startsWith('image/')) {
        window.open(objectUrl, '_blank', 'noopener,noreferrer');
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
      } else {
        const anchor = document.createElement('a');
        anchor.href = objectUrl;
        anchor.download = attachment.filename;
        anchor.click();
        URL.revokeObjectURL(objectUrl);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر فتح المرفق.');
    }
  };

  const timelineItems = useMemo(() => {
    const items: Array<{ key: string; date: string; kind: 'event' | 'message' | 'audit'; content: React.ReactNode }> = [];
    for (const event of timeline?.events ?? []) {
      items.push({
        key: `event-${event.id}`,
        date: event.occurred_at,
        kind: 'event',
        content: <><strong>{event.event_type === 'status_changed' ? 'تغيّرت حالة الشكوى' : event.event_type.replaceAll('_', ' ')}</strong>{event.from_status && event.to_status && <span className="timeline-transition">{STATUS_LABEL[event.from_status]} <Icon name="arrow" size={13} /> {STATUS_LABEL[event.to_status]}</span>}</>,
      });
    }
    for (const message of timeline?.messages ?? []) {
      items.push({
        key: `message-${message.id}`,
        date: message.created_at,
        kind: 'message',
        content: <div className={`timeline-message ${message.visibility === 'internal' ? 'is-internal' : ''}`}><div className="timeline-message-meta"><strong>{message.visibility === 'internal' ? 'ملاحظة داخلية' : message.direction === 'inbound' ? 'رسالة واردة' : 'رد من فريق الوساطة'}</strong><span>{CHANNEL_LABEL[message.channel ?? 'portal']}</span></div><p>{message.body}</p></div>,
      });
    }
    for (const row of audit) {
      items.push({ key: `audit-${row.id}`, date: row.created_at, kind: 'audit', content: <><strong>سجل تدقيق · {row.action.replaceAll('_', ' ')}</strong><span className="timeline-meta">{row.resource_type}{row.actor_staff_id ? ` · ${row.actor_staff_id.slice(0, 8)}` : ''}</span></> });
    }
    return items.sort((left, right) => new Date(left.date).getTime() - new Date(right.date).getTime());
  }, [timeline, audit]);

  if (!hasPermission(access, 'complaints.view')) return <AccessDenied title="الشكوى غير متاحة" />;

  return (
    <div className="page-stack">
      <div className="workspace-back-row"><button className="back-link" type="button" onClick={onBack}><Icon name="arrow" size={16} /> العودة إلى الشكاوى</button><span className="workspace-scope"><Icon name="shield" size={14} /> عرض ضمن النطاق المصرّح</span></div>
      {loading && !ticket ? <div className="loading-surface"><span className="loading-spinner" /><span>جارٍ تحميل الشكوى…</span></div> : !ticket ? (
        <section className="surface-card unavailable-ticket"><span className="empty-icon"><Icon name="lock" size={22} /></span><h2>الشكوى غير متاحة</h2><p>قد تكون الشكوى غير موجودة أو خارج نطاق صلاحيتك. لم يتم عرض أي تفاصيل.</p><button className="button button-dark" type="button" onClick={onBack}>العودة إلى القائمة</button></section>
      ) : (
        <>
          <div className="ticket-heading surface-card">
            <div className="ticket-heading-main"><div className="ticket-breadcrumb"><span>الشكاوى</span><Icon name="chevron" size={13} /><strong>{ticket.ticket_no}</strong></div><h1>{ticket.subject}</h1><div className="ticket-heading-meta"><StatusPill status={ticket.status} /><span className="meta-divider" /> <span>{companyName}</span><span className="meta-dot">·</span><span>{ticket.insurance_type === 'health' ? 'تأمين طبي' : ticket.insurance_type === 'general' ? 'تأمين عام' : 'تأمين مشترك'}</span><span className="meta-dot">·</span><span>وصلت {formatDateTime(ticket.created_at)}</span></div></div>
            <div className="ticket-heading-actions">
              {canAssign && <button className="button button-light" type="button" onClick={() => { setSelectedAgent(ticket.assigned_agent_id ?? ''); setShowAssign(true); }}><Icon name="users" size={15} />{ticket.assigned_agent_id ? 'إعادة إسناد' : 'إسناد'}</button>}
              {hasPermission(access, 'complaints.escalate') && hasPermission(access, 'integrations.send_email') && ticket.status !== 'transferred' && <button className="button button-gold" type="button" onClick={() => setShowTransfer(true)}><Icon name="mail" size={15} /> إرسال للشركة بالبريد</button>}
            </div>
          </div>

          {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void refresh(); }}>إعادة المحاولة</button></div>}
          {notice && <div className="inline-alert success" role="status">{notice}<button className="notice-close" type="button" aria-label="إخفاء الإشعار" onClick={() => setNotice('')}><Icon name="close" size={14} /></button></div>}

          <div className="workspace-grid">
            <div className="workspace-main-column">
              <section className="surface-card customer-summary-card">
                <div className="panel-heading"><div><p className="eyebrow">بيانات الشكوى</p><h2>ملخص الحالة</h2></div><span className="ticket-id-chip">{ticket.ticket_no}</span></div>
                <div className="detail-grid">
                  <div><span>اسم العميل</span><strong>{ticket.customer_name ?? 'عميل'}</strong></div>
                  <div><span>رقم الهوية</span><strong dir="ltr">{ticket.national_id_masked ?? '**************'}</strong></div>
                  <div><span>رقم الوثيقة</span><strong dir="ltr">{ticket.policy_number ?? '—'}</strong></div>
                  <div><span>شركة التأمين</span><strong>{companyName}</strong></div>
                  <div><span>الأولوية</span><strong><span className={`priority-pill priority-${ticket.priority}`}>{ticket.priority === 'urgent' ? 'عاجلة' : ticket.priority === 'high' ? 'مرتفعة' : ticket.priority === 'low' ? 'منخفضة' : 'عادية'}</span></strong></div>
                  <div><span>قناة الوصول</span><strong>{CHANNEL_LABEL[ticket.channel] ?? ticket.channel}</strong></div>
                  <div><span>موظف المتابعة</span><strong>{currentAgent ?? 'غير مُسند'}</strong></div>
                  <div><span>موعد SLA</span><strong className={ticket.sla_breached_at ? 'text-danger' : ''}>{formatDateTime(ticket.sla_due_at)}{ticket.sla_breached_at ? ' · متجاوز' : ''}</strong></div>
                </div>
                <div className="ticket-description"><h3>تفاصيل الشكوى</h3><p>{ticket.description}</p></div>
                {ticket.merged_into_id && <div className="merged-callout">تم دمج هذه الشكوى في سجل آخر ضمن النطاق.</div>}
              </section>

              <section className="surface-card conversation-card">
                <div className="panel-heading"><div><p className="eyebrow">سجل المحادثة</p><h2>الرسائل والتحديثات</h2></div><span className="unified-inbox-badge"><Icon name="inbox" size={14} /> صندوق موحّد</span></div>
                <div className="channel-legend"><span><i className="channel-dot portal" /> بوابة العملاء</span><span><i className="channel-dot email" /> بريد إلكتروني</span><span><i className="channel-dot whatsapp" /> واتساب</span><span><i className="channel-dot call" /> مكالمات</span></div>
                <div className="timeline-list">
                  {timelineItems.map((item) => <article className={`timeline-item timeline-${item.kind}`} key={item.key}><span className={`timeline-marker marker-${item.kind}`}>{item.kind === 'message' ? <Icon name="message" size={13} /> : item.kind === 'audit' ? <Icon name="shield" size={13} /> : <Icon name="check" size={13} />}</span><div className="timeline-item-body"><time>{formatDateTime(item.date)}</time>{item.content}</div></article>)}
                  {timelineItems.length === 0 && <div className="empty-inline">لا توجد رسائل أو تغييرات مسجلة بعد.</div>}
                </div>

                {(hasPermission(access, 'complaints.reply_public') || hasPermission(access, 'complaints.add_internal_note')) && (
                  <form className="reply-composer" onSubmit={(event) => { void sendMessage(event); }}>
                    <div className="composer-tabs">
                      {hasPermission(access, 'complaints.reply_public') && <button className={replyMode === 'public' ? 'active' : ''} type="button" onClick={() => setReplyMode('public')}><Icon name="message" size={14} /> رد علني</button>}
                      {hasPermission(access, 'complaints.add_internal_note') && <button className={replyMode === 'internal' ? 'active internal-active' : ''} type="button" onClick={() => setReplyMode('internal')}><Icon name="lock" size={13} /> ملاحظة داخلية</button>}
                    </div>
                    <textarea aria-label={replyMode === 'public' ? 'اكتب رداً علنياً' : 'اكتب ملاحظة داخلية'} value={reply} onChange={(event) => setReply(event.target.value)} placeholder={replyMode === 'public' ? 'اكتب رداً واضحاً للعميل…' : 'هذه الملاحظة لا تظهر للعميل…'} rows={4} maxLength={5000} />
                    <div className="composer-footer">
                      <div className="composer-tools">
                        {replyMode === 'public' && cannedReplies.length > 0 && <select aria-label="رد جاهز" value="" onChange={(event) => { const canned = cannedReplies.find((item) => item.id === event.target.value); if (canned) setReply(canned.body_ar || canned.body_en); }}><option value="">إدراج رد جاهز</option>{cannedReplies.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>}
                        {hasPermission(access, 'complaints.view_attachments') && <><input ref={attachmentInput} aria-label="اختيار مرفقات" className="visually-hidden" type="file" accept="application/pdf,image/jpeg,image/png" multiple disabled={uploadingAttachments} onChange={(event) => { const selected = event.currentTarget.files; if (selected) void uploadAttachments(selected); event.currentTarget.value = ''; }} /><button className="text-tool" type="button" disabled={uploadingAttachments} onClick={() => attachmentInput.current?.click()}><Icon name="paperclip" size={15} /> {uploadingAttachments ? 'جارٍ الرفع…' : 'مرفق'}</button></>}
                      </div>
                      {hasPermission(access, replyMode === 'public' ? 'complaints.reply_public' : 'complaints.add_internal_note') && <button className="button button-dark" type="submit" disabled={sending || !reply.trim()}>{sending ? 'جارٍ الإرسال…' : replyMode === 'public' ? 'إرسال الرد' : 'إضافة الملاحظة'} <Icon name="send" size={14} /></button>}
                    </div>
                  </form>
                )}
              </section>
            </div>

            <aside className="workspace-side-column">
              <section className="surface-card action-panel">
                <div className="panel-heading"><div><p className="eyebrow">إجراءات سريعة</p><h2>إدارة الحالة</h2></div><Icon name="more" size={17} /></div>
                <div className="status-change-box"><label className="field-label">تغيير الحالة
                  <select aria-label="الحالة الجديدة" value={newStatus} onChange={(event) => setNewStatus(event.target.value as ComplaintStatus)} disabled={!hasPermission(access, 'complaints.change_status')}>
                    {(['assigned', 'in_review', 'resolved'] as ComplaintStatus[]).map((status) => <option key={status} value={status}>{STATUS_LABEL[status]}</option>)}
                  </select>
                </label>{hasPermission(access, 'complaints.change_status') && <button className="button button-dark full-width" type="button" disabled={actionBusy || newStatus === ticket.status} onClick={() => { void updateStatus(); }}>تحديث الحالة <Icon name="arrow" size={14} /></button>}</div>
                <div className="action-list">
                  {hasPermission(access, 'complaints.merge') && <button type="button" className="action-row" onClick={() => setShowDuplicates(true)}><span className="action-row-icon"><Icon name="layers" size={16} /></span><span><strong>مراجعة التكرار والدمج</strong><small>{duplicates.length ? `${formatNumber(duplicates.length)} سجلات مشابهة` : 'فحص السجلات المشابهة'}</small></span><Icon name="chevron" size={14} /></button>}
                  {hasPermission(access, 'integrations.send_whatsapp') && <div className="action-row is-disabled" title="تكامل واتساب غير مفعّل"><span className="action-row-icon whatsapp-icon"><Icon name="message" size={16} /></span><span><strong>إرسال عبر واتساب</strong><small>موفّر واتساب غير متصل</small></span><span className="mini-lock"><Icon name="lock" size={12} /></span></div>}
                  {hasPermission(access, 'integrations.ai_call') && <div className="action-row is-disabled" title="موفّر المكالمات الصوتية غير مفعّل"><span className="action-row-icon call-icon"><Icon name="phone" size={16} /></span><span><strong>اتصال صوتي بالعميل</strong><small>خدمة الصوت الذكي غير مفعّلة</small></span><span className="mini-lock"><Icon name="lock" size={12} /></span></div>}
                </div>
                <div className="action-note"><Icon name="shield" size={14} /> الإجراءات تخضع لفحص الصلاحيات على الخادم.</div>
              </section>

              <section className="surface-card attachments-panel">
                <div className="panel-heading"><div><p className="eyebrow">الملفات</p><h2>المرفقات <span className="heading-count">{formatNumber(attachments.length)}</span></h2></div><Icon name="paperclip" size={17} /></div>
                {!hasPermission(access, 'complaints.view_attachments') ? <p className="muted small-copy">لا تتوفر لديك صلاحية عرض المرفقات.</p> : attachments.length === 0 ? <p className="muted small-copy">لا توجد مرفقات مرتبطة بهذه الشكوى.</p> : <div className="attachment-list">{attachments.map((attachment) => { const scanLabel = { clean: 'آمن', pending: 'معلّق', infected: 'مصاب', error: 'خطأ في الفحص' }[attachment.scan_status]; const scanHint = { clean: 'تم الفحص', pending: 'بانتظار الفحص', infected: 'تم رفض الملف', error: 'تعذر إكمال الفحص' }[attachment.scan_status]; return <button className="attachment-row" key={attachment.id} type="button" disabled={attachment.scan_status !== 'clean'} title={attachment.scan_status !== 'clean' ? 'المرفق غير متاح للفتح حتى يصبح آمناً' : 'فتح المرفق'} onClick={() => { void downloadAttachment(attachment); }}><span className="attachment-file-icon"><Icon name="paperclip" size={15} /></span><span className="attachment-copy"><strong>{attachment.filename}</strong><small>{(attachment.size_bytes / 1024).toFixed(0)} KB · {scanHint}</small></span><span className={`scan-state scan-${attachment.scan_status}`}>{scanLabel}</span></button>; })}</div>}
              </section>

              <section className="surface-card call-panel">
                <div className="panel-heading"><div><p className="eyebrow">قنوات التواصل</p><h2>سجل المكالمات</h2></div><span className="muted">—</span></div>
                <div className="call-empty"><span className="call-empty-icon"><Icon name="phone" size={18} /></span><div><strong>لا توجد مكالمات مسجلة</strong><small>سيظهر السجل والتفريغ الصوتي عند تفعيل موفّر الاتصال.</small></div></div>
                {hasPermission(access, 'integrations.ai_call') && <button className="button button-light full-width" type="button" disabled title="موفّر المكالمات غير مفعّل"><Icon name="phone" size={14} /> اتصال العميل عبر الصوت الذكي</button>}
              </section>

              <section className="surface-card ai-reply-card"><div className="ai-icon small-ai"><Icon name="spark" size={16} /></div><div><strong>رد مقترح بالذكاء الاصطناعي</strong><p>ستظهر الاقتراحات بعد إعداد موفّر الذكاء الاصطناعي. لا يوجد رد آلي مُنشأ.</p></div><span className="ai-unavailable-label">غير مفعّل</span></section>
            </aside>
          </div>

          {showAssign && <Modal title={ticket.assigned_agent_id ? 'إعادة إسناد الشكوى' : 'إسناد الشكوى'} eyebrow="توزيع العمل" onClose={() => setShowAssign(false)}><p className="modal-intro">اختر موظفاً من قائمة الشكاوى الظاهرة ضمن نطاق صلاحيتك.</p><form onSubmit={(event) => { void assign(event); }}><label className="field-label">الموظف المسؤول<select autoFocus value={selectedAgent} onChange={(event) => setSelectedAgent(event.target.value)} required><option value="">اختر موظفاً</option>{options.agents.map((agent: AgentOption) => <option key={agent.id} value={agent.id}>{agent.full_name}</option>)}</select></label><div className="modal-actions"><button className="button button-light" type="button" onClick={() => setShowAssign(false)}>إلغاء</button><button className="button button-dark" type="submit" disabled={actionBusy || !selectedAgent}>تأكيد الإسناد</button></div></form></Modal>}
          {showTransfer && <Modal title="إرسال إلى شركة التأمين" eyebrow="تحويل آمن بالبريد" onClose={() => setShowTransfer(false)}><div className="transfer-notice"><Icon name="mail" size={18} /><span>سيتم تحويل الحالة وإنشاء إشعار بريد إلى جهة الاتصال المسجلة للشركة. الإرسال الفعلي يعتمد على إعداد موفّر البريد.</span></div><form onSubmit={(event) => { void transfer(event); }}><label className="field-label">سبب التحويل<textarea autoFocus rows={4} minLength={10} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="اكتب ملخصاً واضحاً لمبررات الإحالة (١٠ أحرف على الأقل)…" required /></label><div className="modal-actions"><button className="button button-light" type="button" onClick={() => setShowTransfer(false)}>إلغاء</button><button className="button button-gold" type="submit" disabled={actionBusy || reason.trim().length < 10}>تحويل وإرسال</button></div></form></Modal>}
          {showDuplicates && <Modal title="سجلات قد تكون مكررة" eyebrow="مراجعة يدوية" onClose={() => setShowDuplicates(false)} wide><p className="modal-intro">لا يتم الدمج تلقائياً. راجع كل سجل واختر دمج الشكوى الحالية فيه عند التأكد.</p>{duplicates.length === 0 ? <div className="empty-inline">لم يتم العثور على شكاوى مشابهة ضمن نطاقك.</div> : <div className="duplicate-list">{duplicates.map((candidate) => <article className="duplicate-card" key={candidate.id}><div><strong>{candidate.ticket_no}</strong><StatusPill status={candidate.status} /><p>{candidate.subject}</p><small>{formatDateTime(candidate.created_at)}</small></div><button className="button button-light button-small" type="button" disabled={actionBusy} onClick={() => { void mergeInto(candidate.id); }}>دمج في هذا السجل</button><button className="text-link-button" type="button" onClick={() => { setShowDuplicates(false); onOpenTicket(candidate.id); }}>فتح <Icon name="external" size={13} /></button></article>)}</div>}</Modal>}
        </>
      )}
    </div>
  );
}
