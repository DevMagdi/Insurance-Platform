import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { staffApi } from '../lib/api';
import { navigate } from '../lib/router';
import { hasPermission, type StaffComplaintFilters } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { EmptyState, ErrorState, Field, LoadingState, SelectInput, StatusChip, TextInput, ghostButton } from '../components/ui';

const PAGE_SIZE = 20;

export function StaffDashboardPage() {
  const { t, i18n } = useTranslation();
  const { access } = useStaffMe();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<StaffComplaintFilters>({ page: 1, page_size: PAGE_SIZE, sort_by: 'created_at', sort_dir: 'desc' });
  const [selected, setSelected] = useState<string[]>([]);
  const [viewName, setViewName] = useState('');
  const [agentId, setAgentId] = useState('');
  const [bulkStatus, setBulkStatus] = useState('assigned');
  const [message, setMessage] = useState('');
  const [live, setLive] = useState(false);

  const options = useQuery({ queryKey: ['staff-filter-options'], queryFn: staffApi.filterOptions, enabled: Boolean(access) });
  const counters = useQuery({ queryKey: ['staff-counters', filters], queryFn: () => staffApi.counters(filters), enabled: Boolean(access) });
  const complaints = useQuery({ queryKey: ['staff-complaints', filters], queryFn: () => staffApi.complaints(filters), enabled: Boolean(access) });
  const views = useQuery({ queryKey: ['staff-views'], queryFn: staffApi.views, enabled: Boolean(access) });
  const notifications = useQuery({ queryKey: ['staff-notifications'], queryFn: staffApi.notifications, enabled: Boolean(access) });

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['staff-complaints'] });
    await queryClient.invalidateQueries({ queryKey: ['staff-counters'] });
  };

  useEffect(() => {
    if (!access || !hasPermission(access, 'complaints.view')) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${window.location.host}/api/staff/complaints/stream`);
    socket.onopen = () => setLive(true);
    socket.onmessage = () => {
      setLive(true);
      void queryClient.invalidateQueries({ queryKey: ['staff-complaints'] });
      void queryClient.invalidateQueries({ queryKey: ['staff-counters'] });
    };
    socket.onclose = () => setLive(false);
    socket.onerror = () => setLive(false);
    return () => socket.close();
  }, [access, queryClient]);

  const bulkMutation = useMutation({
    mutationFn: async (action: 'assign' | 'status') => {
      if (action === 'assign') {
        return staffApi.bulk({ action, complaint_ids: selected, assignment: { agent_id: agentId, team_id: null }, status: null });
      }
      return staffApi.bulk({ action, complaint_ids: selected, assignment: null, status: bulkStatus });
    },
    onSuccess: async () => {
      setSelected([]);
      setMessage('✓');
      await refresh();
    },
  });

  const viewMutation = useMutation({
    mutationFn: () => staffApi.createView(viewName, filters),
    onSuccess: async () => {
      setViewName('');
      await queryClient.invalidateQueries({ queryKey: ['staff-views'] });
    },
  });

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil((complaints.data?.total ?? 0) / PAGE_SIZE)),
    [complaints.data?.total],
  );

  const update = (patch: Partial<StaffComplaintFilters>) => {
    setSelected([]);
    setFilters((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));
  };

  if (!hasPermission(access, 'complaints.view')) {
    return <StaffShell><EmptyState message={t('staff.noAccess')} /></StaffShell>;
  }

  return (
    <StaffShell>
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-xl font-black">{t('staff.queue')}</h2>
            <p className="text-sm text-slate-500 dark:text-slate-300">
              {counters.data ? `${counters.data.total} · ${counters.data.open} open · ${counters.data.resolved} resolved` : ''}
              {' · '}
              <span className={live ? 'font-bold text-emerald-500' : 'text-slate-400'}>{live ? t('common.live') : t('common.offline')}</span>
            </p>
          </div>
          <div className="flex gap-2">
            {hasPermission(access, 'complaints.export') && (
              <button type="button" className={ghostButton} onClick={() => void staffApi.downloadComplaintsCsv(filters)}>
                CSV
              </button>
            )}
            <button type="button" className={ghostButton} onClick={() => void refresh()}>
              {t('common.retry')}
            </button>
          </div>
        </div>

        <div className="mt-5 grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Field label={t('common.search')}>
            <TextInput value={filters.search ?? ''} onChange={(e) => update({ search: e.target.value || undefined })} placeholder="CMP-2026-…" />
          </Field>
          <Field label={t('staff.status')}>
            <SelectInput value={filters.status ?? ''} onChange={(e) => update({ status: (e.target.value || undefined) as StaffComplaintFilters['status'] })}>
              <option value="">{t('status.all')}</option>
              {(options.data?.statuses ?? []).map((status) => <option key={status} value={status}>{t(`status.${status}`)}</option>)}
            </SelectInput>
          </Field>
          <Field label={t('staff.company')}>
            <SelectInput value={filters.company_id ?? ''} onChange={(e) => update({ company_id: e.target.value || undefined })}>
              <option value="">—</option>
              {(options.data?.companies ?? []).map((company) => (
                <option key={company.id} value={company.id}>{i18n.language === 'ar' ? company.name_ar : company.name_en}</option>
              ))}
            </SelectInput>
          </Field>
          <Field label={t('staff.priority')}>
            <SelectInput value={filters.priority ?? ''} onChange={(e) => update({ priority: (e.target.value || undefined) as StaffComplaintFilters['priority'] })}>
              <option value="">—</option>
              {(options.data?.priorities ?? []).map((priority) => <option key={priority} value={priority}>{priority}</option>)}
            </SelectInput>
          </Field>
          <Field label={t('staff.from')}>
            <TextInput type="datetime-local" value={filters.date_from?.slice(0, 16) ?? ''} onChange={(e) => update({ date_from: e.target.value ? new Date(e.target.value).toISOString() : undefined })} />
          </Field>
          <Field label={t('staff.to')}>
            <TextInput type="datetime-local" value={filters.date_to?.slice(0, 16) ?? ''} onChange={(e) => update({ date_to: e.target.value ? new Date(e.target.value).toISOString() : undefined })} />
          </Field>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <TextInput aria-label={t('staff.viewName')} className="max-w-56" value={viewName} onChange={(e) => setViewName(e.target.value)} placeholder={t('staff.viewName')} />
          <button type="button" className={ghostButton} disabled={viewName.trim().length < 2 || viewMutation.isPending} onClick={() => viewMutation.mutate()}>
            {t('common.save')} · {t('staff.savedViews')}
          </button>
          {(views.data ?? []).map((view) => (
            <button
              key={view.id}
              type="button"
              className="rounded-full bg-amber-400/20 px-3 py-1.5 text-sm font-semibold"
              onClick={() => setFilters({ ...view.criteria, page: 1, page_size: PAGE_SIZE })}
            >
              {view.name}
            </button>
          ))}
        </div>

        {selected.length > 0 && (
          <div className="mt-4 flex flex-wrap items-end gap-3 rounded-2xl bg-slate-950/5 p-3 dark:bg-white/10">
            <span className="pb-2 text-sm font-bold">{selected.length} {t('staff.selected')}</span>
            <Field label={t('staff.agentId')}>
              <TextInput value={agentId} onChange={(e) => setAgentId(e.target.value)} placeholder="00000000-0000-…" />
            </Field>
            <button type="button" className={ghostButton} disabled={!agentId || bulkMutation.isPending} onClick={() => bulkMutation.mutate('assign')}>
              {t('staff.bulkAssign')}
            </button>
            <Field label={t('staff.bulkStatus')}>
              <SelectInput value={bulkStatus} onChange={(e) => setBulkStatus(e.target.value)}>
                <option value="assigned">{t('status.assigned')}</option>
                <option value="in_review">{t('status.in_review')}</option>
                <option value="resolved">{t('status.resolved')}</option>
              </SelectInput>
            </Field>
            <button type="button" className={ghostButton} disabled={bulkMutation.isPending} onClick={() => bulkMutation.mutate('status')}>
              {t('staff.apply')}
            </button>
            {message && <span className="pb-2 font-bold text-emerald-500">{message}</span>}
          </div>
        )}

        <div className="mt-5 overflow-x-auto">
          {complaints.isLoading ? (
            <LoadingState />
          ) : complaints.isError ? (
            <ErrorState message={t('staff.loadError')} onRetry={() => void complaints.refetch()} />
          ) : (complaints.data?.items.length ?? 0) === 0 ? (
            <EmptyState message={t('tickets.empty')} />
          ) : (
            <table className="w-full min-w-[760px] text-start text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500 dark:border-white/10 dark:text-slate-300">
                  <th className="py-3 pe-3"><span className="sr-only">select</span></th>
                  <th className="py-3 pe-3">{t('staff.ticket')}</th>
                  <th className="py-3 pe-3">{t('detail.subject')}</th>
                  <th className="py-3 pe-3">{t('staff.status')}</th>
                  <th className="py-3 pe-3">{t('staff.priority')}</th>
                  <th className="py-3">{t('staff.created')}</th>
                </tr>
              </thead>
              <tbody>
                {(complaints.data?.items ?? []).map((item) => (
                  <tr key={item.id} className="border-b border-slate-100 transition hover:bg-amber-300/10 dark:border-white/5">
                    <td className="py-3 pe-3">
                      <input
                        type="checkbox"
                        aria-label={item.ticket_no}
                        checked={selected.includes(item.id)}
                        onChange={(e) => setSelected((current) => e.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))}
                      />
                    </td>
                    <td className="py-3 pe-3">
                      <button type="button" className="font-bold text-amber-600 dark:text-amber-300" onClick={() => navigate(`/staff/tickets/${item.id}`)}>
                        {item.ticket_no}
                      </button>
                    </td>
                    <td className="max-w-sm truncate py-3 pe-3">{item.subject}</td>
                    <td className="py-3 pe-3"><StatusChip status={item.status} /></td>
                    <td className="py-3 pe-3">{item.priority}</td>
                    <td className="py-3">{new Date(item.created_at).toLocaleString(i18n.language === 'ar' ? 'ar-EG' : 'en-GB')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="mt-4 flex items-center justify-between">
          <button type="button" className={ghostButton} disabled={(filters.page ?? 1) <= 1} onClick={() => update({ page: (filters.page ?? 1) - 1 })}>
            {t('tickets.prev')}
          </button>
          <span className="text-sm text-slate-500 dark:text-slate-300">{filters.page ?? 1} / {totalPages}</span>
          <button type="button" className={ghostButton} disabled={(filters.page ?? 1) >= totalPages} onClick={() => update({ page: (filters.page ?? 1) + 1 })}>
            {t('tickets.nextPage')}
          </button>
        </div>
      </section>

      {(notifications.data?.length ?? 0) > 0 && (
        <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
          <h3 className="font-black">{t('staff.notifications')}</h3>
          <ul className="mt-3 space-y-2">
            {notifications.data?.slice(0, 8).map((item) => (
              <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-slate-950/5 px-3 py-2 text-sm dark:bg-white/10">
                <span>{item.kind} · {new Date(item.created_at).toLocaleString()}</span>
                {!item.read_at && (
                  <button type="button" className="font-bold text-amber-600 dark:text-amber-300" onClick={() => void staffApi.markNotificationRead(item.id).then(() => notifications.refetch())}>
                    {t('profile.markRead')}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </StaffShell>
  );
}
