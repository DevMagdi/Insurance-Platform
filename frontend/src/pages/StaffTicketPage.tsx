import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ApiError, staffApi } from '../lib/api';
import { hasPermission } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { StatusTimeline } from '../components/StatusTimeline';
import { EmptyState, ErrorState, Field, LoadingState, SelectInput, StatusChip, TextArea, TextInput, ghostButton, primaryButton } from '../components/ui';

export function StaffTicketPage({ complaintId }: { complaintId: string }) {
  const { t, i18n } = useTranslation();
  const { access } = useStaffMe();
  const queryClient = useQueryClient();
  const [reply, setReply] = useState('');
  const [note, setNote] = useState('');
  const [status, setStatus] = useState('in_review');
  const [agentId, setAgentId] = useState('');
  const [teamId, setTeamId] = useState('');
  const [escalationReason, setEscalationReason] = useState('');
  const [targetId, setTargetId] = useState('');
  const [revealReason, setRevealReason] = useState('');
  const [nationalId, setNationalId] = useState('');
  const [error, setError] = useState('');

  const detail = useQuery({ queryKey: ['staff-ticket', complaintId], queryFn: () => staffApi.complaint(complaintId) });
  const timeline = useQuery({ queryKey: ['staff-ticket-timeline', complaintId], queryFn: () => staffApi.timeline(complaintId) });
  const attachments = useQuery({
    queryKey: ['staff-ticket-attachments', complaintId],
    queryFn: () => staffApi.attachments(complaintId),
    enabled: hasPermission(access, 'complaints.view_attachments'),
  });
  const duplicates = useQuery({ queryKey: ['staff-ticket-duplicates', complaintId], queryFn: () => staffApi.duplicates(complaintId) });
  const canned = useQuery({
    queryKey: ['staff-ticket-canned', complaintId],
    queryFn: () => staffApi.cannedReplies(complaintId),
    enabled: hasPermission(access, 'complaints.reply_public'),
  });
  const ai = useQuery({ queryKey: ['staff-ticket-ai', complaintId], queryFn: () => staffApi.aiInsights(complaintId) });

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['staff-ticket', complaintId] });
    await queryClient.invalidateQueries({ queryKey: ['staff-ticket-timeline', complaintId] });
    await queryClient.invalidateQueries({ queryKey: ['staff-complaints'] });
  };

  const action = useMutation({
    mutationFn: async (work: () => Promise<unknown>) => work(),
    onSuccess: invalidate,
    onError: (caught) => setError(caught instanceof ApiError ? caught.detail : t('errors.generic')),
  });

  const aiMutation = useMutation({
    mutationFn: () => staffApi.generateAi(complaintId),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['staff-ticket-ai', complaintId] }),
    onError: (caught) => setError(caught instanceof ApiError ? caught.detail : t('errors.generic')),
  });

  if (detail.isLoading) return <StaffShell><LoadingState /></StaffShell>;
  if (detail.isError || !detail.data) {
    return <StaffShell><ErrorState message={t('staff.loadError')} onRetry={() => void detail.refetch()} /></StaffShell>;
  }

  const item = detail.data;
  const payloadText = (payload: Record<string, unknown>, keys: string[]) =>
    keys.map((key) => payload[key]).find((value) => typeof value === 'string') as string | undefined;

  return (
    <StaffShell>
      {error && <ErrorState message={error} />}
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <button type="button" className="text-sm font-bold text-amber-600 dark:text-amber-300" onClick={() => window.history.back()}>
              ← {t('staff.queue')}
            </button>
            <h2 className="mt-2 text-2xl font-black">{item.ticket_no}</h2>
            <p className="mt-1 font-semibold">{item.subject}</p>
            <p className="mt-2 max-w-3xl whitespace-pre-wrap text-slate-600 dark:text-slate-300">{item.description}</p>
          </div>
          <StatusChip status={item.status} />
        </div>
        <dl className="mt-5 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div><dt className="text-slate-500">{t('staff.customer')}</dt><dd className="font-bold">{item.customer_name}</dd></div>
          <div><dt className="text-slate-500">{t('profile.nationalId')}</dt><dd className="font-bold" dir="ltr">{nationalId || item.national_id_masked}</dd></div>
          <div><dt className="text-slate-500">{t('staff.company')}</dt><dd className="font-bold">{item.company_name}</dd></div>
          <div><dt className="text-slate-500">{t('detail.policy')}</dt><dd className="font-bold" dir="ltr">{item.policy_number}</dd></div>
          <div><dt className="text-slate-500">{t('staff.priority')}</dt><dd className="font-bold">{item.priority}</dd></div>
          <div><dt className="text-slate-500">SLA</dt><dd className="font-bold">{item.sla_due_at ? new Date(item.sla_due_at).toLocaleString() : '—'}</dd></div>
          <div><dt className="text-slate-500">{t('staff.assign')}</dt><dd className="font-bold">{item.assigned_agent_name ?? item.assigned_agent_id ?? '—'}</dd></div>
          <div><dt className="text-slate-500">CSAT</dt><dd className="font-bold">{item.csat_score ?? '—'}</dd></div>
        </dl>
        {hasPermission(access, 'complaints.view_full_national_id') && (
          <div className="mt-4 flex flex-wrap items-end gap-2">
            <Field label={t('staff.reveal')}>
              <TextInput value={revealReason} onChange={(e) => setRevealReason(e.target.value)} placeholder={t('staff.reason')} />
            </Field>
            <button
              type="button"
              className={ghostButton}
              disabled={revealReason.trim().length < 10 || action.isPending}
              onClick={() => void staffApi.revealNationalId(complaintId, revealReason).then((result) => setNationalId(result.national_id)).catch((caught: unknown) => setError(caught instanceof ApiError ? caught.detail : t('errors.generic')))}
            >
              {t('staff.reveal')}
            </button>
          </div>
        )}
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
        <section className="space-y-5">
          <div className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('timeline.title')}</h3>
            <div className="mt-4"><StatusTimeline status={item.status} events={timeline.data?.events ?? []} /></div>
            <div className="mt-5 space-y-3">
              {(timeline.data?.messages ?? []).map((message) => (
                <article key={message.id} className={`rounded-2xl p-3 text-sm ${message.visibility === 'internal' ? 'bg-amber-300/15' : 'bg-slate-950/5 dark:bg-white/10'}`}>
                  <p className="whitespace-pre-wrap">{message.body}</p>
                  <p className="mt-1 text-xs text-slate-500">{message.visibility} · {message.direction} · {new Date(message.created_at).toLocaleString()}</p>
                </article>
              ))}
              {(timeline.data?.messages.length ?? 0) === 0 && <EmptyState message={t('detail.noMessages')} />}
            </div>
          </div>

          <div className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('files.title')}</h3>
            <ul className="mt-3 space-y-2">
              {(attachments.data ?? []).map((file) => (
                <li key={file.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-slate-950/5 px-3 py-2 text-sm dark:bg-white/10">
                  <span>{file.filename} · {file.scan_status}</span>
                  {file.scan_status === 'clean' && (
                    <button type="button" className="font-bold text-amber-600 dark:text-amber-300" onClick={() => void staffApi.downloadAttachment(file.id, file.filename)}>
                      {t('common.download')}
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {(attachments.data?.length ?? 0) === 0 && <EmptyState message="—" />}
          </div>
        </section>

        <aside className="space-y-5">
          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('staff.status')}</h3>
            <div className="mt-3 flex gap-2">
              <SelectInput value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="assigned">{t('status.assigned')}</option>
                <option value="in_review">{t('status.in_review')}</option>
                <option value="resolved">{t('status.resolved')}</option>
                <option value="transferred">{t('status.transferred')}</option>
              </SelectInput>
              <button type="button" className={primaryButton} disabled={action.isPending} onClick={() => action.mutate(() => staffApi.status(complaintId, status))}>
                {t('staff.apply')}
              </button>
            </div>
            <div className="mt-5 space-y-3">
              <Field label={t('staff.agentId')}><TextInput value={agentId} onChange={(e) => setAgentId(e.target.value)} /></Field>
              <Field label={t('staff.teamId')}><TextInput value={teamId} onChange={(e) => setTeamId(e.target.value)} /></Field>
              <div className="flex gap-2">
                <button type="button" className={ghostButton} disabled={!agentId || action.isPending} onClick={() => action.mutate(() => staffApi.assign(complaintId, agentId, teamId || undefined))}>{t('staff.assign')}</button>
                <button type="button" className={ghostButton} disabled={!agentId || action.isPending} onClick={() => action.mutate(() => staffApi.assign(complaintId, agentId, teamId || undefined, true))}>↻</button>
              </div>
            </div>
            <div className="mt-5 space-y-3">
              <Field label={t('staff.escalate')}><TextArea value={escalationReason} onChange={(e) => setEscalationReason(e.target.value)} /></Field>
              <button type="button" className={ghostButton} disabled={escalationReason.trim().length < 10 || action.isPending} onClick={() => action.mutate(() => staffApi.escalate(complaintId, escalationReason))}>
                {t('staff.escalate')}
              </button>
              <Field label={t('staff.merge')}><TextInput value={targetId} onChange={(e) => setTargetId(e.target.value)} /></Field>
              <button type="button" className={ghostButton} disabled={!targetId || action.isPending} onClick={() => action.mutate(() => staffApi.merge(complaintId, targetId))}>
                {t('staff.merge')}
              </button>
            </div>
          </section>

          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('staff.publicReply')}</h3>
            {(canned.data?.length ?? 0) > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                {canned.data?.map((itemReply) => (
                  <button key={itemReply.id} type="button" className="rounded-full bg-slate-950/5 px-3 py-1 text-xs font-bold dark:bg-white/10" onClick={() => setReply(i18n.language === 'ar' ? itemReply.body_ar : itemReply.body_en)}>
                    {itemReply.title}
                  </button>
                ))}
              </div>
            )}
            <TextArea className="mt-3" value={reply} onChange={(e) => setReply(e.target.value)} placeholder={t('detail.reply')} />
            <button type="button" className={`${primaryButton} mt-3`} disabled={!reply.trim() || action.isPending} onClick={() => action.mutate(async () => { await staffApi.reply(complaintId, reply); setReply(''); })}>
              {t('staff.send')}
            </button>
            <h3 className="mt-6 font-black">{t('staff.internalNote')}</h3>
            <TextArea className="mt-3" value={note} onChange={(e) => setNote(e.target.value)} />
            <button type="button" className={`${ghostButton} mt-3`} disabled={!note.trim() || action.isPending} onClick={() => action.mutate(async () => { await staffApi.note(complaintId, note); setNote(''); })}>
              {t('staff.send')}
            </button>
          </section>

          <section className="rounded-3xl border border-amber-300/30 bg-gradient-to-br from-amber-300/15 to-emerald-300/10 p-5 shadow-xl backdrop-blur dark:border-white/10">
            <h3 className="font-black">✦ {t('staff.ai')}</h3>
            <button type="button" className={`${primaryButton} mt-3 w-full`} disabled={aiMutation.isPending} onClick={() => aiMutation.mutate()}>
              {t('staff.generateAi')}
            </button>
            <div className="mt-4 space-y-3">
              {(ai.data ?? []).slice(0, 8).map((insight) => (
                <article key={insight.id} className="rounded-2xl bg-white/70 p-3 text-sm dark:bg-slate-950/30">
                  <div className="flex items-center justify-between gap-2">
                    <strong>{insight.kind}</strong>
                    <span className="text-xs">{insight.review_status} · {insight.provider_model}</span>
                  </div>
                  <p className="mt-2 whitespace-pre-wrap">
                    {payloadText(insight.payload, ['summary_ar', 'summary_en', 'reply_ar', 'reply_en', 'label_ar', 'label_en', 'sentiment', 'category_code']) ?? JSON.stringify(insight.payload)}
                  </p>
                  {insight.kind === 'reply' && payloadText(insight.payload, ['reply_en']) && (
                    <button type="button" className="mt-2 font-bold text-amber-600 dark:text-amber-300" onClick={() => setReply(payloadText(insight.payload, [i18n.language === 'ar' ? 'reply_ar' : 'reply_en']) ?? '')}>
                      {t('staff.publicReply')}
                    </button>
                  )}
                  {insight.review_status === 'suggested' && (
                    <div className="mt-2 flex gap-2">
                      <button type="button" className="font-bold text-emerald-600" onClick={() => void staffApi.reviewAi(complaintId, insight.id, 'accepted').then(() => ai.refetch())}>{t('staff.accept')}</button>
                      <button type="button" className="font-bold text-rose-500" onClick={() => void staffApi.reviewAi(complaintId, insight.id, 'rejected').then(() => ai.refetch())}>{t('staff.reject')}</button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          </section>

          {(duplicates.data?.length ?? 0) > 0 && (
            <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
              <h3 className="font-black">{t('staff.duplicates')}</h3>
              <pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs" dir="ltr">{JSON.stringify(duplicates.data, null, 2)}</pre>
            </section>
          )}
        </aside>
      </div>
    </StaffShell>
  );
}
