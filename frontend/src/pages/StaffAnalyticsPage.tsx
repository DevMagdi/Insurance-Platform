import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, staffApi } from '../lib/api';
import { hasPermission, type StaffComplaintFilters } from '../lib/staffTypes';
import { isValidDateWindow, toIsoDateTime } from '../lib/validation';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { EmptyState, ErrorState, Field, LoadingState, TextInput } from '../components/ui';

const KPI_LABELS: Record<string, string> = {
  total: 'إجمالي الشكاوى',
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم حلها',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
  avg_resolution_hours: 'متوسط زمن الحل (ساعة)',
  sla_breaches: 'تجاوزات اتفاقية الخدمة',
  reopen_rate: 'معدل إعادة الفتح',
  csat_avg: 'متوسط رضا العملاء',
};

const STATUS_LABELS: Record<string, string> = {
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم حلها',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
};

function formatKpi(key: string, value: number | null): string {
  if (value === null || Number.isNaN(value)) return '—';
  if (key === 'reopen_rate') return `${(value * 100).toFixed(1)}٪`;
  if (key === 'csat_avg') return `${value.toFixed(1)} / 5`;
  if (key === 'avg_resolution_hours') return value.toFixed(1);
  return value.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function defaultWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 29 * 86400000);
  return { date_from: start.toISOString(), date_to: end.toISOString() };
}

export function StaffAnalyticsPage() {
  const { t } = useTranslation();
  const { access } = useStaffMe();
  const [window, setWindow] = useState<StaffComplaintFilters>(defaultWindow);
  // The analytics payload carries English insurer names; the screen is Arabic.
  const companies = useQuery({ queryKey: ['companies'], queryFn: api.companies, staleTime: 300_000 });
  const companyNameAr = (id: string, fallback: string) =>
    companies.data?.find((item) => item.id === id)?.name_ar || fallback;
  // The API requires a complete window of at most 366 days; without it the
  // request is rejected, so it is never sent.
  const windowValid = isValidDateWindow(window.date_from ?? '', window.date_to ?? '');
  const query = useQuery({
    queryKey: ['staff-analytics', window],
    queryFn: () => staffApi.analytics({ ...window, date_from: window.date_from ?? '', date_to: window.date_to ?? '' }),
    enabled: hasPermission(access, 'analytics.view') && windowValid,
  });

  if (!hasPermission(access, 'analytics.view')) {
    return <StaffShell><EmptyState message={t('staff.noAccess')} /></StaffShell>;
  }

  const data = query.data;
  const maxDaily = Math.max(1, ...(data?.time_series.map((item) => item.arrived) ?? [1]));

  return (
    <StaffShell>
      <section className="card panel-lg">
        <h2 className="text-2xl font-bold">{t('staff.analytics')}</h2>
        <p className="mt-1 text-sm text-text-muted">{t('staff.dateWindow')}</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label={t('staff.from')}>
            <TextInput type="datetime-local" value={window.date_from?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_from: toIsoDateTime(e.target.value) }))} />
          </Field>
          <Field label={t('staff.to')}>
            <TextInput type="datetime-local" value={window.date_to?.slice(0, 16)} onChange={(e) => setWindow((w) => ({ ...w, date_to: toIsoDateTime(e.target.value) }))} />
          </Field>
        </div>
        {!windowValid ? (
          <p className="mt-4 text-sm text-amber-700 dark:text-amber-300">حدّد تاريخ بداية ونهاية صالحين، وبفارق لا يتجاوز 366 يوماً.</p>
        ) : query.isLoading ? <LoadingState /> : query.isError || !data ? (
          <ErrorState message={t('staff.loadError')} onRetry={() => void query.refetch()} />
        ) : (
          <>
            <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {Object.entries(data.kpis).slice(0, 12).map(([key, value]) => (
                <article key={key} className="card card-inset p-5">
                  <p className="text-xs font-bold tracking-wide text-text-muted">{KPI_LABELS[key] ?? key}</p>
                  <p className="mt-1 text-3xl font-bold tabular"><bdi dir="ltr">{typeof value === 'number' || value === null ? formatKpi(key, value) : value}</bdi></p>
                </article>
              ))}
            </div>

            <h3 className="mt-7 font-black">الشكاوى الواردة يومياً</h3>
            <div className="mt-3 flex h-40 items-end gap-1 overflow-x-auto" aria-label="daily arrivals">
              {data.time_series.map((item) => (
                <div key={item.date} className="flex min-w-3 flex-1 flex-col items-center gap-1">
                  <div className="w-full rounded-t" style={{ height: `${Math.max(4, (item.arrived / maxDaily) * 120)}px`, background: 'linear-gradient(to top, var(--gold-600), var(--gold-300))' }} title={`${item.date}: ${item.arrived}`} />
                  <span className="hidden text-[9px] text-text-muted xl:block">{item.date.slice(5)}</span>
                </div>
              ))}
            </div>

            <div className="mt-7 grid gap-5 lg:grid-cols-3">
              <article>
                <h3 className="font-black">{t('staff.status')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.by_status.map((item) => <li key={item.status} className="flex justify-between"><span>{STATUS_LABELS[item.status] ?? item.status}</span><strong>{item.count.toLocaleString('en-US')}</strong></li>)}</ul>
              </article>
              <article>
                <h3 className="font-black">{t('staff.company')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.by_company.map((item) => <li key={item.company_id} className="flex justify-between gap-3"><span>{companyNameAr(item.company_id, item.name)}</span><strong>{item.count.toLocaleString('en-US')}</strong></li>)}</ul>
              </article>
              <article>
                <h3 className="font-black">{t('staff.catalog')}</h3>
                <ul className="mt-2 space-y-1 text-sm">{data.top_issues.map((item) => <li key={item.category} className="flex justify-between gap-3"><span>{item.category}</span><strong>{item.count.toLocaleString('en-US')}</strong></li>)}</ul>
              </article>
            </div>

            {(data.company_scorecards.length > 0) && (
              <div className="mt-7 overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead><tr className="border-b border-border text-start text-text-muted"><th className="py-2">{t('staff.company')}</th><th>الإجمالي</th><th>تجاوزات SLA</th><th>رضا العميل</th><th>متوسط الساعات</th></tr></thead>
                  <tbody>
                    {data.company_scorecards.map((row, index) => (
                      <tr key={index} className="border-b border-border">
                        <td className="py-2 font-bold">{companyNameAr(String(row.company_id ?? ''), String(row.name ?? ''))}</td>
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
