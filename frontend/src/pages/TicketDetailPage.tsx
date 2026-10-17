import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  ArrowRight,
  Building2,
  CalendarDays,
  CircleDot,
  Download,
  FileText,
  LockKeyhole,
  MessageSquareText,
  Paperclip,
  RefreshCcw,
  RotateCcw,
  Send,
  ShieldCheck,
} from 'lucide-react';
import { api } from '../lib/api';
import { uploadFiles } from '../lib/api';
import { toQueuedFile, type QueuedFile } from '../lib/files';
import { formatDate } from '../lib/status';
import { navigate } from '../lib/router';
import { FileUploader } from '../components/FileUploader';
import { StatusTimeline } from '../components/StatusTimeline';
import { Button, EmptyState, ErrorState, LoadingState, StatusChip, TextArea } from '../components/ui';
import { LoginPage } from './LoginPage';
import { useMe } from '../hooks/useMe';

function CsatBox({ complaintId, alreadyRated }: { complaintId: string; alreadyRated: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [score, setScore] = useState(0);
  const [comment, setComment] = useState('');
  const [done, setDone] = useState(alreadyRated);
  const csat = useMutation({
    mutationFn: () => api.csat(complaintId, score, comment),
    onSuccess: () => {
      setDone(true);
      void queryClient.invalidateQueries({ queryKey: ['complaint', complaintId] });
    },
    onError: () => setDone(true), // 409 = already submitted on the server.
  });
  if (done) {
    return (
      <p className="alert alert-success" role="status">
        {t(alreadyRated ? 'detail.csatAlready' : 'detail.csatThanks')}
      </p>
    );
  }
  return (
    <section className="card csat-card">
      <h2>{t('detail.csatTitle')}</h2>
      <p>{t('detailExtra.csatDesc')}</p>
      <div className="star-row" role="radiogroup" aria-label={t('detail.csatTitle')}>
        {[1, 2, 3, 4, 5].map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={score === value}
            aria-label={`${value} / 5`}
            onClick={() => setScore(value)}
            className={`star-btn${value <= score ? ' is-active' : ''}`}
          >
            ★
          </button>
        ))}
      </div>
      <TextArea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder={t('detail.csatComment')}
        style={{ marginTop: 12 }}
        maxLength={1000}
      />
      <Button
        style={{ marginTop: 12 }}
        disabled={score === 0 || csat.isPending}
        loading={csat.isPending}
        onClick={() => csat.mutate()}
      >
        {t('detail.csatSend')}
      </Button>
    </section>
  );
}

export function TicketDetailPage({ complaintId }: { complaintId: string }) {
  const { t, i18n } = useTranslation();
  const { me, isLoading: meLoading } = useMe();
  const queryClient = useQueryClient();
  const [replyBody, setReplyBody] = useState('');
  const [queue, setQueue] = useState<QueuedFile[]>([]);
  const [live, setLive] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const Back = i18n.language === 'ar' ? ArrowRight : ArrowLeft;

  const detailQuery = useQuery({
    queryKey: ['complaint', complaintId],
    queryFn: () => api.complaint(complaintId),
    enabled: Boolean(me),
  });
  const timelineQuery = useQuery({
    queryKey: ['complaint-timeline', complaintId],
    queryFn: () => api.timeline(complaintId),
    enabled: Boolean(me),
    refetchInterval: live ? false : 30_000,
  });
  const attachmentsQuery = useQuery({
    queryKey: ['complaint-attachments', complaintId],
    queryFn: () => api.attachments(complaintId),
    enabled: Boolean(me),
  });

  // Real-time updates via SSE (cookie-authenticated). Any event simply
  // invalidates the scoped queries; polling stays as a quiet fallback.
  useEffect(() => {
    if (!me) return;
    const source = new EventSource(`/api/customer/complaints/${complaintId}/stream`);
    const refresh = () => {
      setLive(true);
      void queryClient.invalidateQueries({ queryKey: ['complaint', complaintId] });
      void queryClient.invalidateQueries({ queryKey: ['complaint-timeline', complaintId] });
      void queryClient.invalidateQueries({ queryKey: ['complaint-attachments', complaintId] });
    };
    source.addEventListener('snapshot', refresh);
    source.addEventListener('update', refresh);
    source.onerror = () => setLive(false);
    return () => source.close();
  }, [complaintId, me, queryClient]);

  const reply = useMutation({
    mutationFn: () => api.reply(complaintId, replyBody.trim()),
    onSuccess: () => {
      setReplyBody('');
      void queryClient.invalidateQueries({ queryKey: ['complaint-timeline', complaintId] });
    },
  });

  const reopen = useMutation({
    mutationFn: () => api.reopen(complaintId),
    onSuccess: () => {
      setNotice(t('detail.reopened'));
      void queryClient.invalidateQueries({ queryKey: ['complaint', complaintId] });
      void queryClient.invalidateQueries({ queryKey: ['complaint-timeline', complaintId] });
    },
  });

  const uploadImmediate = async (incoming: File[]) => {
    const queued = incoming.map(toQueuedFile);
    setQueue((current) => [...current, ...queued]);
    const uploading = queued.map((q) => ({ ...q, status: 'uploading' as const }));
    setQueue((current) => current.map((q) => uploading.find((u) => u.id === q.id) ?? q));
    const results = await uploadFiles(complaintId, incoming, (index, percent) => {
      const target = queued[index];
      setQueue((current) => current.map((q) => (q.id === target.id ? { ...q, progress: percent } : q)));
    });
    results.forEach((result, index) => {
      const target = queued[index];
      setQueue((current) =>
        current.map((q) => (q.id === target.id ? { ...q, status: result.ok ? 'done' : 'error', error: result.error } : q)),
      );
    });
    void queryClient.invalidateQueries({ queryKey: ['complaint-attachments', complaintId] });
  };

  const retryQueued = async (id: string) => {
    const target = queue.find((q) => q.id === id);
    if (!target) return;
    setQueue((current) => current.filter((q) => q.id !== id));
    await uploadImmediate([target.file]);
  };

  if (meLoading) return <LoadingState />;
  if (!me) return <LoginPage />;
  if (detailQuery.isLoading) return <LoadingState />;
  if (detailQuery.isError || !detailQuery.data) {
    return <ErrorState message={t('errors.generic')} onRetry={() => void detailQuery.refetch()} />;
  }

  const detail = detailQuery.data;
  const timeline = timelineQuery.data;
  const messages = (timeline?.messages ?? []).filter((m) => m.visibility === 'public');
  const showCsat = detail.can_csat && (detail.status === 'resolved' || detail.csat_score !== null);
  const attachments = attachmentsQuery.data ?? [];

  return (
    <div>
      <div className="detail-top">
        <button type="button" className="back-link" onClick={() => navigate('/tickets')}>
          <Back size={14} />
          {t('commonExtra.backToTickets')}
        </button>
        <span className={`live-badge${live ? ' is-live' : ''}`} role="status">
          <span className={`dot${live ? ' dot-live' : ''}`} aria-hidden="true" />
          {live ? t('detailExtra.liveUpdates') : t('detailExtra.polling')}
        </span>
      </div>

      <section className="detail-hero">
        <span className="detail-hero-band" aria-hidden="true" />
        <div className="detail-hero-body">
          <div className="detail-hero-top">
            <span className="detail-ticket-no" dir="ltr">
              <CircleDot size={12} aria-hidden="true" />
              {detail.ticket_no}
            </span>
            <StatusChip status={detail.status} />
          </div>
          <h1>{detail.subject}</h1>
          <div className="detail-hero-meta">
            <span><Building2 size={12} /> {detail.company_name}</span>
            <span><FileText size={12} /> {t('detail.policy')} <b dir="ltr">{detail.policy_number}</b></span>
            <span><ShieldCheck size={12} /> {detail.assigned_agent_name ?? t('timeline.noAgent')}</span>
            <span><CalendarDays size={12} /> {formatDate(detail.created_at, i18n.language)}</span>
          </div>
          <p className="detail-hero-desc">{detail.description}</p>
          {notice && <p className="alert alert-success" role="status" style={{ marginTop: 14 }}>{notice}</p>}
          <div className="detail-hero-actions">
            {detail.can_reopen && (
              <Button variant="secondary" loading={reopen.isPending} onClick={() => reopen.mutate()} leadingIcon={<RotateCcw size={14} />}>
                {t('detail.reopen')}
              </Button>
            )}
            <Button
              variant="ghost"
              onClick={() => {
                void detailQuery.refetch();
                void timelineQuery.refetch();
                void attachmentsQuery.refetch();
              }}
              leadingIcon={<RefreshCcw size={14} />}
            >
              {t('common.refresh')}
            </Button>
          </div>
        </div>
      </section>

      <div className="detail-grid">
        <div className="detail-col">
          <StatusTimeline status={detail.status} events={timeline?.events ?? []} />

          {showCsat && <CsatBox complaintId={complaintId} alreadyRated={detail.csat_score !== null} />}

          <section className="card" aria-labelledby="conversation-title">
            <div className="panel-head">
              <h2 id="conversation-title"><MessageSquareText size={14} /> {t('detailExtra.conversationTitle')}</h2>
              <span className="chip">{messages.length}</span>
            </div>
            <div className="conversation">
              <div className="message-list">
                {messages.length === 0 && <EmptyState message={t('detail.noMessages')} />}
                {messages.map((message) => {
                  const mine = message.direction === 'inbound';
                  return (
                    <div key={message.id} className={`message ${mine ? 'inbound' : 'outbound'}`}>
                      <span className="message-avatar" aria-hidden="true">{mine ? t('detail.you').slice(0, 2) : 'W'}</span>
                      <div>
                        <div className="message-author">
                          <span>{mine ? t('detail.you') : t('detail.team')}</span>
                          <time>{formatDate(message.created_at, i18n.language)}</time>
                        </div>
                        <div className="message-bubble">
                          <p>{message.body}</p>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="composer">
                <TextArea
                  value={replyBody}
                  onChange={(e) => setReplyBody(e.target.value)}
                  placeholder={t('detailExtra.replyPlaceholder')}
                  aria-label={t('detail.reply')}
                  maxLength={5000}
                />
                <div className="composer-foot">
                  <span>
                    {detail.status === 'resolved' ? t('detailExtra.replyLocked') : t('detailExtra.replyHint')}
                  </span>
                  <Button
                    size="sm"
                    disabled={replyBody.trim().length === 0 || detail.status === 'resolved'}
                    loading={reply.isPending}
                    onClick={() => reply.mutate()}
                    trailingIcon={<Send size={13} />}
                  >
                    {t('detailExtra.sendReply')}
                  </Button>
                </div>
                {reply.isError && <p className="field-error" role="alert">{t('errors.generic')}</p>}
              </div>
            </div>
          </section>
        </div>

        <aside className="detail-side">
          <section className="card" aria-labelledby="attachments-title">
            <div className="panel-head">
              <h2 id="attachments-title"><Paperclip size={14} /> {t('detailExtra.attachmentsTitle')}</h2>
              <span className="chip">{attachments.length}</span>
            </div>
            {attachments.length > 0 ? (
              <div className="attachment-list">
                {attachments.map((file) => (
                  <div key={file.id} className="attachment-row">
                    <span className="attachment-icon" aria-hidden="true"><FileText size={14} /></span>
                    <span className="attachment-copy">
                      <strong title={file.filename}>{file.filename}</strong>
                      <span>{file.scan_status === 'clean' ? t('files.clean') : t('files.pending')}</span>
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={file.scan_status !== 'clean'}
                      title={file.scan_status !== 'clean' ? t('files.blocked') : undefined}
                      onClick={() => void api.downloadAttachment(file.id, file.filename)}
                      aria-label={`${t('common.download')} ${file.filename}`}
                    >
                      <Download size={14} />
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="panel-body" style={{ color: 'var(--ink-400)', fontSize: 11.5 }}>{t('detailExtra.noAttachments')}</p>
            )}
            <div className="panel-body">
              <FileUploader
                files={queue}
                onAdd={(incoming) => void uploadImmediate(incoming)}
                onRemove={(id) => setQueue((current) => current.filter((q) => q.id !== id))}
                onRetry={(id) => void retryQueued(id)}
              />
            </div>
          </section>

          <section className="card">
            <div className="panel-head"><h2>{t('detailExtra.overview')}</h2></div>
            <dl className="side-list">
              <div className="side-row">
                <dt>{t('detailExtra.company')}</dt>
                <dd>{detail.company_name}</dd>
              </div>
              <div className="side-row">
                <dt>{t('detailExtra.policy')}</dt>
                <dd dir="ltr">{detail.policy_number}</dd>
              </div>
              <div className="side-row">
                <dt>{t('detailExtra.assignedAgent')}</dt>
                <dd>{detail.assigned_agent_name ?? t('detailExtra.noAgent')}</dd>
              </div>
              <div className="side-row">
                <dt>{t('detailExtra.opened')}</dt>
                <dd>{formatDate(detail.created_at, i18n.language)}</dd>
              </div>
            </dl>
          </section>

          <p className="detail-security-note">
            <LockKeyhole size={13} /> {t('trackExtra.securityNote')}
          </p>
        </aside>
      </div>
    </div>
  );
}
