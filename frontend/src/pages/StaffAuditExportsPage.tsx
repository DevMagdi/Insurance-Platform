import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { staffApi } from '../lib/api';
import { hasPermission, type StaffComplaintFilters } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { EmptyState, ErrorState, Field, LoadingState, TextInput, ghostButton, primaryButton } from '../components/ui';

function defaultWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 29 * 86400000);
  return { start: start.toISOString(), end: end.toISOString() };
}

export function StaffAuditPage() {
  const { t } = useTranslation();
  const { access } = useStaffMe();
  const [window, setWindow] = useState(defaultWindow);
  const events = useQuery({
    queryKey: ['staff-audit', window],
    queryFn: () => staffApi.auditEvents(window.start, window.end),
    enabled: hasPermission(access, 'audit.view'),
  });
  const changes = useQuery({
    queryKey: ['staff-access-changes'],
    queryFn: staffApi.accessChanges,
    enabled: hasPermission(access, 'audit.view') && hasPermission(access, 'users.manage_access'),
  });

  if (!hasPermission(access, 'audit.view')) return <StaffShell><EmptyState message={t('staff.noAccess')} /></StaffShell>;

  return (
    <StaffShell>
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h2 className="text-xl font-black">{t('staff.audit')}</h2>
          <button type="button" className={ghostButton} onClick={() => void staffApi.downloadAuditCsv(window.start, window.end)}>CSV</button>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label={t('staff.from')}><TextInput type="datetime-local" value={window.start.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, start: new Date(e.target.value).toISOString() }))} /></Field>
          <Field label={t('staff.to')}><TextInput type="datetime-local" value={window.end.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, end: new Date(e.target.value).toISOString() }))} /></Field>
        </div>
        {events.isLoading ? <LoadingState /> : events.isError ? <ErrorState message={t('staff.loadError')} onRetry={() => void events.refetch()} /> : (
          <div className="mt-5 overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead><tr className="border-b text-start text-slate-500 dark:border-white/10"><th className="py-2">Action</th><th>Resource</th><th>Actor</th><th>{t('staff.created')}</th></tr></thead>
              <tbody>
                {(events.data ?? []).map((row, index) => (
                  <tr key={String(row.id ?? index)} className="border-b border-slate-100 dark:border-white/5">
                    <td className="py-2 font-bold">{String(row.action ?? '')}</td>
                    <td>{String(row.resource_type ?? '')}</td>
                    <td dir="ltr" className="text-xs">{String(row.actor_staff_id ?? '')}</td>
                    <td>{row.created_at ? new Date(String(row.created_at)).toLocaleString() : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(changes.data?.length ?? 0) > 0 && (
          <>
            <h3 className="mt-7 font-black">{t('staff.access')}</h3>
            <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-2xl bg-slate-950/5 p-3 text-xs dark:bg-white/10" dir="ltr">{JSON.stringify(changes.data, null, 2)}</pre>
          </>
        )}
      </section>
    </StaffShell>
  );
}

const EXPORT_KIND: Record<string, string> = { complaints: 'الشكاوى', analytics: 'التحليلات' };
const EXPORT_STATUS: Record<string, string> = {
  queued: 'في الانتظار',
  running: 'قيد التجهيز',
  completed: 'جاهز للتنزيل',
  failed: 'فشل',
  expired: 'منتهي',
};

export function StaffExportsPage() {
  const { t } = useTranslation();
  const { access } = useStaffMe();
  const queryClient = useQueryClient();
  const [window, setWindow] = useState<StaffComplaintFilters>(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 29 * 86400000);
    return { date_from: start.toISOString(), date_to: end.toISOString(), page: 1, page_size: 100 };
  });
  const jobs = useQuery({ queryKey: ['staff-exports'], queryFn: staffApi.exports, enabled: Boolean(access) });
  const create = useMutation({
    mutationFn: (kind: 'complaints' | 'analytics') => staffApi.createExport(kind, window),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['staff-exports'] }),
  });

  if (!hasPermission(access, 'complaints.export') && !hasPermission(access, 'analytics.export')) {
    return <StaffShell><EmptyState message={t('staff.noAccess')} /></StaffShell>;
  }

  return (
    <StaffShell>
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="text-xl font-black">{t('staff.exports')}</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-300">{t('staff.dateWindow')}</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label={t('staff.from')}><TextInput type="datetime-local" value={window.date_from?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_from: new Date(e.target.value).toISOString() }))} /></Field>
          <Field label={t('staff.to')}><TextInput type="datetime-local" value={window.date_to?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_to: new Date(e.target.value).toISOString() }))} /></Field>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" className={primaryButton} disabled={create.isPending} onClick={() => create.mutate('complaints')}>تصدير الشكاوى (CSV)</button>
          <button type="button" className={ghostButton} disabled={create.isPending} onClick={() => create.mutate('analytics')}>تصدير التحليلات (CSV)</button>
          <button type="button" className={ghostButton} onClick={() => void jobs.refetch()}>{t('common.retry')}</button>
        </div>
        <h3 className="mt-7 font-black">أحدث عمليات التصدير</h3>
        {jobs.isLoading ? <LoadingState /> : jobs.isError ? <ErrorState message={t('staff.loadError')} /> : (jobs.data ?? []).length === 0 ? (
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-300">لا توجد عمليات تصدير بعد. اختر الفترة ثم ابدأ تصديراً جديداً.</p>
        ) : (
          <ul className="mt-3 space-y-2">
            {(jobs.data ?? []).map((job) => (
              <li key={job.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-slate-950/5 px-3 py-2 text-sm dark:bg-white/10">
                <span>{EXPORT_KIND[job.kind] ?? job.kind} · {EXPORT_STATUS[job.status] ?? job.status} · {new Date(job.created_at).toLocaleString('ar-EG')}</span>
                {job.status === 'completed' && (
                  <button type="button" className="font-bold text-amber-600 dark:text-amber-300" onClick={() => void staffApi.downloadExport(job.id)}>{t('common.download')}</button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </StaffShell>
  );
}
