import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { staffApi } from '../lib/api';
import { hasPermission, type StaffComplaintFilters } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { EmptyState, ErrorState, Field, LoadingState, TextInput } from '../components/ui';

function defaultWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 29 * 86400000);
  return { date_from: start.toISOString(), date_to: end.toISOString() };
}

export function StaffAnalyticsPage() {
  const { t } = useTranslation();
  const { access } = useStaffMe();
  const [window, setWindow] = useState<StaffComplaintFilters>(defaultWindow);
  const query = useQuery({
    queryKey: ['staff-analytics', window],
    queryFn: () => staffApi.analytics(window),
    enabled: hasPermission(access, 'analytics.view'),
  });

  if (!hasPermission(access, 'analytics.view')) {
    return <StaffShell><EmptyState message={t('staff.noAccess')} /></StaffShell>;
  }

  const data = query.data;
  const maxDaily = Math.max(1, ...(data?.time_series.map((item) => item.arrived) ?? [1]));

  return (
    <StaffShell>
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="text-xl font-black">{t('staff.analytics')}</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-300">{t('staff.dateWindow')}</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label={t('staff.from')}>
            <TextInput type="datetime-local" value={window.date_from?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_from: new Date(e.target.value).toISOString() }))} />
          </Field>
          <Field label={t('staff.to')}>
            <TextInput type="datetime-local" value={window.date_to?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_to: new Date(e.target.value).toISOString() }))} />
          </Field>
        </div>
        {query.isLoading ? <LoadingState /> : query.isError || !data ? (
          <ErrorState message={t('staff.loadError')} onRetry={() => void query.refetch()} />
        ) : (
          <>
            <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {Object.entries(data.kpis).slice(0, 12).map(([key, value]) => (
                <article key={key} className="rounded-2xl bg-slate-950/5 p-4 dark:bg-white/10">
                  <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-300">{key}</p>
                  <p className="mt-1 text-3xl font-black">{value === null ? '—' : typeof value === 'number' ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : value}</p>
                </article>
              ))}
            </div>

            <h3 className="mt-7 font-black">{t('tickets.title')}</h3>
            <div className="mt-3 flex h-40 items-end gap-1 overflow-x-auto" aria-label="daily arrivals">
              {data.time_series.map((item) => (
                <div key={item.date} className="flex min-w-3 flex-1 flex-col items-center gap-1">
                  <div className="w-full rounded-t bg-gradient-to-t from-emerald-500 to-amber-400" style={{ height: `${Math.max(4, (item.arrived / maxDaily) * 120)}px` }} title={`${item.date}: ${item.arrived}`} />
                  <span className="hidden text-[9px] text-slate-400 xl:block">{item.date.slice(5)}</span>
                </div>
              ))}
            </div>

            <div className="mt-7 grid gap-5 lg:grid-cols-3">
              <article>
                <h3 className="font-black">{t('staff.status')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.by_status.map((item) => <li key={item.status} className="flex justify-between"><span>{item.status}</span><strong>{item.count}</strong></li>)}</ul>
              </article>
              <article>
                <h3 className="font-black">{t('staff.company')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.by_company.map((item) => <li key={item.company_id} className="flex justify-between gap-3"><span>{item.name}</span><strong>{item.count}</strong></li>)}</ul>
              </article>
              <article>
                <h3 className="font-black">{t('staff.catalog')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.top_issues.map((item) => <li key={item.category} className="flex justify-between gap-3"><span>{item.category}</span><strong>{item.count}</strong></li>)}</ul>
              </article>
            </div>

            {(data.company_scorecards.length > 0) && (
              <div className="mt-7 overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead><tr className="border-b text-start text-slate-500 dark:border-white/10"><th className="py-2">{t('staff.company')}</th><th>Total</th><th>SLA</th><th>CSAT</th><th>Avg hours</th></tr></thead>
                  <tbody>
                    {data.company_scorecards.map((row, index) => (
                      <tr key={index} className="border-b border-slate-100 dark:border-white/5">
                        <td className="py-2 font-bold">{String(row.name ?? '')}</td>
                        <td>{String(row.total ?? '')}</td>
                        <td>{String(row.sla_breaches ?? '')}</td>
                        <td>{row.csat_avg == null ? '—' : Number(row.csat_avg).toFixed(2)}</td>
                        <td>{row.avg_resolution_hours == null ? '—' : Number(row.avg_resolution_hours).toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </StaffShell>
  );
}
