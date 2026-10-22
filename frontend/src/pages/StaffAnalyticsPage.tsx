import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, staffApi } from '../lib/api';
import { hasPermission, type StaffComplaintFilters } from '../lib/staffTypes';
import { isValidDateWindow, toIsoDateTime } from '../lib/validation';
import { formatNumber } from '../utils/date';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { Icon, type IconName } from '../components/Icon';
import { Blank, Metric, Panel, Stagger, StaggerItem } from '../components/ui/Premium';
import { EmptyState, Field, TextInput } from '../components/ui';

/**
 * Every KPI the overview endpoint can return, in Arabic. A key that is missing
 * here used to fall through to `KPI_LABELS[key] ?? key` and print the raw
 * column name — the screen shipped with a card literally headed
 * `avg_first_response_minutes`. Unknown keys are now dropped instead of
 * leaking the schema into the UI.
 */
const KPI_LABELS: Record<string, string> = {
  total: 'إجمالي الطلبات',
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم حلها',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
  avg_resolution_hours: 'متوسط زمن الحل',
  avg_first_response_minutes: 'متوسط زمن أول رد',
  sla_breaches: 'تجاوزات اتفاقية الخدمة',
  reopen_rate: 'معدل إعادة الفتح',
  csat_avg: 'متوسط رضا العملاء',
  escalated: 'تم تصعيدها',
  closed: 'مغلقة',
  overdue: 'متأخرة',
};

const KPI_HINT: Record<string, string> = {
  avg_resolution_hours: 'بالساعات',
  avg_first_response_minutes: 'بالدقائق',
  csat_avg: 'من ٥',
  reopen_rate: 'من إجمالي الطلبات',
};

/** The four numbers that lead the page, in the order they are read. */
const HEADLINE = ['total', 'resolved', 'sla_breaches', 'avg_resolution_hours'] as const;

const HEADLINE_META: Record<string, { icon: IconName; tone: 'violet' | 'success' | 'danger' | 'info' }> = {
  total: { icon: 'inbox', tone: 'violet' },
  resolved: { icon: 'check', tone: 'success' },
  sla_breaches: { icon: 'shield', tone: 'danger' },
  avg_resolution_hours: { icon: 'clock', tone: 'info' },
};

const STATUS_LABELS: Record<string, string> = {
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم حلها',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
};

/** Arabic-Indic like every other number in the product. */
function formatKpi(key: string, value: number | null): string {
  if (value === null || Number.isNaN(value)) return '—';
  if (key === 'reopen_rate') return `${formatNumber(Math.round(value * 1000) / 10, 1)}٪`;
  if (key === 'csat_avg') return `${formatNumber(Math.round(value * 10) / 10, 1)} / ٥`;
  if (key === 'avg_resolution_hours' || key === 'avg_first_response_minutes') {
    return formatNumber(Math.round(value * 10) / 10, 1);
  }
  return formatNumber(Math.round(value));
}

function defaultWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 29 * 86400000);
  return { date_from: start.toISOString(), date_to: end.toISOString() };
}

/** `2026-10-18` → `١٠/١٨`, so the axis matches the rest of the page. */
function axisLabel(iso: string): string {
  const [, month, day] = iso.split('-');
  if (!month || !day) return iso;
  return `${formatNumber(Number(month))}/${formatNumber(Number(day))}`;
}

/**
 * A labelled share of a total. Three of these replace what used to be three
 * bare `<ul>`s of `label … number` with no sense of proportion.
 */
function ShareRow({ label, value, max }: { label: string; value: number; max: number }) {
  return (
    <li className="share-row">
      <span className="share-label" title={label}>{label}</span>
      <span className="share-track" aria-hidden="true">
        <span className="share-fill" style={{ width: `${Math.max(3, (value / Math.max(1, max)) * 100)}%` }} />
      </span>
      <strong className="share-value">{formatNumber(value)}</strong>
    </li>
  );
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
  const kpis = (data?.kpis ?? {}) as Record<string, number | null>;
  const secondary = Object.keys(kpis)
    .filter((key) => KPI_LABELS[key] && !HEADLINE.includes(key as (typeof HEADLINE)[number]))
    .slice(0, 8);

  return (
    <StaffShell>
      <header className="analytics-head">
        <div>
          <p className="eyebrow">{t('staff.analytics')}</p>
          <h1>الأداء خلال الفترة المحددة</h1>
          <p className="analytics-sub">{t('staff.dateWindow')}</p>
        </div>
      </header>

      <div className="ds-filters analytics-window">
        <Field label={t('staff.from')}>
          <TextInput
            type="datetime-local"
            value={window.date_from?.slice(0, 16)}
            onChange={(e) => setWindow((w) => ({ ...w, date_from: toIsoDateTime(e.target.value) }))}
          />
        </Field>
        <Field label={t('staff.to')}>
          <TextInput
            type="datetime-local"
            value={window.date_to?.slice(0, 16)}
            onChange={(e) => setWindow((w) => ({ ...w, date_to: toIsoDateTime(e.target.value) }))}
          />
        </Field>
      </div>

      {!windowValid ? (
        <Panel>
          <Blank
            icon="calendar"
            title="الفترة الزمنية غير صالحة"
            body="حدّد تاريخ بداية ونهاية صالحين، وبفارق لا يتجاوز ٣٦٦ يوماً."
          />
        </Panel>
      ) : query.isLoading ? (
        <>
          <div className="ds-kpi-grid">
            {[0, 1, 2, 3].map((i) => <div key={i} className="ds-skeleton ds-skeleton-card" />)}
          </div>
          <Panel><div className="ds-loading-lines"><span /><span /><span /></div></Panel>
        </>
      ) : query.isError || !data ? (
        <Panel>
          <Blank
            icon="alert"
            title={t('staff.loadError')}
            body="تعذر تحميل مؤشرات الأداء. قد تكون الفترة طويلة أو الاتصال منقطعاً."
            action={<button type="button" className="button button-gold" onClick={() => void query.refetch()}>إعادة المحاولة</button>}
          />
        </Panel>
      ) : (
        <>
          <Stagger className="ds-kpi-grid">
            {HEADLINE.map((key) => (
              <StaggerItem key={key}>
                <Metric
                  label={KPI_LABELS[key]}
                  value={formatKpi(key, kpis[key] ?? null)}
                  hint={KPI_HINT[key]}
                  icon={<Icon name={HEADLINE_META[key].icon} size={16} />}
                  tone={HEADLINE_META[key].tone}
                />
              </StaggerItem>
            ))}
          </Stagger>

          {secondary.length > 0 && (
            <Panel>
              <ul className="analytics-secondary">
                {secondary.map((key) => (
                  <li key={key}>
                    <span>{KPI_LABELS[key]}</span>
                    <strong>{formatKpi(key, kpis[key] ?? null)}</strong>
                    {KPI_HINT[key] && <small>{KPI_HINT[key]}</small>}
                  </li>
                ))}
              </ul>
            </Panel>
          )}

          <Panel>
            <div className="panel-heading">
              <div>
                <p className="eyebrow">الاتجاه</p>
                <h2>الطلبات الواردة يومياً</h2>
              </div>
              <Icon name="chart" size={17} />
            </div>
            {data.time_series.length === 0 ? (
              <Blank icon="chart" title="لا توجد بيانات في هذه الفترة" body="وسّع الفترة الزمنية لرؤية الاتجاه." />
            ) : (
              /* Neutral columns, with the accent spent once — on the busiest
                 day. A chart where every bar is violet says nothing. */
              <div className="trend-chart" aria-label="الطلبات الواردة يومياً">
                {data.time_series.map((item) => (
                  <div className="trend-col" key={item.date} title={`${item.date}: ${item.arrived}`}>
                    <span className="trend-count">{formatNumber(item.arrived)}</span>
                    <span
                      className={`trend-bar${item.arrived === maxDaily ? ' is-peak' : ''}`}
                      style={{ height: `${Math.max(4, (item.arrived / maxDaily) * 120)}px` }}
                    />
                    <span className="trend-label">{axisLabel(item.date)}</span>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <div className="analytics-shares">
            {([
              { title: t('staff.status'), rows: data.by_status.map((i) => ({ key: i.status, label: STATUS_LABELS[i.status] ?? i.status, value: i.count })) },
              { title: t('staff.company'), rows: data.by_company.map((i) => ({ key: i.company_id, label: companyNameAr(i.company_id, i.name), value: i.count })) },
              { title: t('staff.catalog'), rows: data.top_issues.map((i) => ({ key: i.category, label: i.category, value: i.count })) },
            ]).map((group) => {
              const max = Math.max(1, ...group.rows.map((r) => r.value));
              return (
                <Panel key={group.title}>
                  <div className="panel-heading"><div><h2>{group.title}</h2></div></div>
                  {group.rows.length === 0 ? (
                    <p className="share-none">لا توجد بيانات.</p>
                  ) : (
                    <ul className="share-list">
                      {group.rows.map((row) => <ShareRow key={row.key} label={row.label} value={row.value} max={max} />)}
                    </ul>
                  )}
                </Panel>
              );
            })}
          </div>

          {data.company_scorecards.length > 0 && (
            <Panel flush>
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">تفصيل</p>
                  <h2>أداء شركات التأمين</h2>
                </div>
              </div>
              <div className="ds-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('staff.company')}</th>
                      <th>الإجمالي</th>
                      <th>تجاوزات SLA</th>
                      <th>رضا العميل</th>
                      <th>متوسط الساعات</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.company_scorecards.map((row, index) => (
                      <tr key={index}>
                        <td className="subject-cell"><strong>{companyNameAr(String(row.company_id ?? ''), String(row.name ?? ''))}</strong></td>
                        <td>{row.total == null ? '—' : formatNumber(Number(row.total))}</td>
                        <td>{row.sla_breaches == null ? '—' : formatNumber(Number(row.sla_breaches))}</td>
                        <td>{row.csat_avg == null ? '—' : formatNumber(Math.round(Number(row.csat_avg) * 100) / 100, 2)}</td>
                        <td>{row.avg_resolution_hours == null ? '—' : formatNumber(Math.round(Number(row.avg_resolution_hours) * 10) / 10, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )}
        </>
      )}
    </StaffShell>
  );
}
