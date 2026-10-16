import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../lib/api';
import { uploadFiles } from '../lib/api';
import { toQueuedFile, type QueuedFile } from '../lib/files';
import { formatDate } from '../lib/status';
import { navigate } from '../lib/router';
import { FileUploader } from '../components/FileUploader';
import { StatusTimeline } from '../components/StatusTimeline';
import { EmptyState, ErrorState, LoadingState, StatusChip, TextArea, ghostButton, primaryButton } from '../components/ui';
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
    return <p className="rounded-2xl bg-emerald-500/10 px-4 py-3 text-sm text-emerald-600 dark:text-emerald-300" role="status">{t(alreadyRated ? 'detail.csatAlready' : 'detail.csatThanks')}</p>;
  }
  return (
    <section className="rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5">
      <h2 className="font-semibold">{t('detail.csatTitle')}</h2>
      <div className="mt-3 flex gap-1" role="radiogroup" aria-label={t('detail.csatTitle')}>
        {[1, 2, 3, 4, 5].map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={score === value}
            aria-label={`${value} / 5`}
            onClick={() => setScore(value)}
            className={`text-3xl transition ${value <= score ? 'text-amber-400' : 'text-slate-300 dark:text-slate-600'}`}
          >
            ★
          </button>
        ))}
      </div>
      <TextArea value={comment} onChange={(e) => setComment(e.target.value)} placeholder={t('detail.csatComment')} className="mt-3" maxLength={1000} />
      <button type="button" className={`${primaryButton} mt-3`} disabled={score === 0 || csat.isPending} onClick={() => csat.mutate()}>
        {t('detail.csatSend')}
      </button>
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

  return (
    <section className="space-y-5">
      <button type="button" className={ghostButton} onClick={() => navigate('/tickets')}>
        ← {t('nav.tickets')}
      </button>

      <div className="rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-mono font-bold" dir="ltr">{detail.ticket_no}</span>
          <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${live ? 'text-emerald-500' : 'text-slate-400'}`} role="status">
            <span className={`h-1.5 w-1.5 rounded-full ${live ? 'animate-pulse bg-emerald-400' : 'bg-slate-400'}`} />
            {live ? t('common.live') : t('common.offline')}
          </span>
        </div>
        <h1 className="mt-2 text-xl font-bold">{detail.subject}</h1>
        <p className="mt-2 whitespace-pre-wrap leading-7 text-slate-600 dark:text-slate-300">{detail.description}</p>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-slate-500">{t('detail.company')}</dt>
            <dd className="font-medium">{detail.company_name}</dd>
          </div>
          <div>
            <dt className="text-slate-500">{t('detail.policy')}</dt>
            <dd className="font-medium" dir="ltr">{detail.policy_number}</dd>
          </div>
          <div>
            <dt className="text-slate-500">{t('timeline.agent')}</dt>
            <dd className="font-medium">{detail.assigned_agent_name ?? t('timeline.noAgent')}</dd>
          </div>
        </dl>
        <div className="mt-3"><StatusChip status={detail.status} /></div>
        {notice && <p className="mt-3 rounded-xl bg-emerald-500/10 px-3 py-2 text-sm text-emerald-600 dark:text-emerald-300" role="status">{notice}</p>}
        {detail.can_reopen && (
          <button type="button" className={`${ghostButton} mt-4`} disabled={reopen.isPending} onClick={() => reopen.mutate()}>
            {t('detail.reopen')}
          </button>
        )}
      </div>

      <StatusTimeline status={detail.status} events={timeline?.events ?? []} />

      {showCsat && <CsatBox complaintId={complaintId} alreadyRated={detail.csat_score !== null} />}

      <section className="rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="mb-4 font-semibold">{t('timeline.events')} · {t('detail.reply')}</h2>
        <div className="space-y-3">
          {messages.length === 0 && <EmptyState message={t('detail.noMessages')} />}
          {messages.map((message) => {
            const mine = message.direction === 'inbound';
            return (
              <div key={message.id} className={`flex ${mine ? 'justify-start' : 'justify-end'}`}>
                <div className={`max-w-[85%] rounded-2xl px-4 py-3 ${mine ? 'bg-amber-400/15' : 'bg-emerald-500/10'}`}>
                  <p className="text-xs font-semibold text-slate-500">{mine ? t('detail.you') : t('detail.team')}</p>
                  <p className="mt-1 whitespace-pre-wrap leading-6">{message.body}</p>
                  <time className="mt-1 block text-[11px] text-slate-400">{formatDate(message.created_at, i18n.language)}</time>
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-4 flex gap-2">
          <TextArea
            value={replyBody}
            onChange={(e) => setReplyBody(e.target.value)}
            placeholder={t('detail.reply')}
            aria-label={t('detail.reply')}
            className="min-h-12"
            maxLength={5000}
          />
          <button
            type="button"
            className={primaryButton}
            disabled={replyBody.trim().length === 0 || reply.isPending || detail.status === 'resolved'}
            onClick={() => reply.mutate()}
          >
            {t('detail.sendReply')}
          </button>
        </div>
        {reply.isError && <p className="mt-2 text-sm text-rose-500" role="alert">{t('errors.generic')}</p>}
      </section>

      <section className="rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="mb-3 font-semibold">{t('files.title')}</h2>
        {(attachmentsQuery.data ?? []).length > 0 && (
          <ul className="mb-4 space-y-2">
            {attachmentsQuery.data?.map((file) => (
              <li key={file.id} className="flex items-center justify-between gap-3 rounded-xl bg-slate-900/[0.03] px-3 py-2 text-sm dark:bg-white/5">
                <span className="min-w-0">
                  <span className="block truncate font-medium">{file.filename}</span>
                  <span className="text-xs text-slate-500">
                    {file.scan_status === 'clean' ? t('files.clean') : t('files.pending')}
                  </span>
                </span>
                <button
                  type="button"
                  className={ghostButton}
                  disabled={file.scan_status !== 'clean'}
                  title={file.scan_status !== 'clean' ? t('files.blocked') : undefined}
                  onClick={() => void api.downloadAttachment(file.id, file.filename)}
                >
                  {t('common.download')}
                </button>
              </li>
            ))}
          </ul>
        )}
        <FileUploader
          files={queue}
          onAdd={(incoming) => void uploadImmediate(incoming)}
          onRemove={(id) => setQueue((current) => current.filter((q) => q.id !== id))}
          onRetry={(id) => void retryQueued(id)}
        />
      </section>
    </section>
  );
}
