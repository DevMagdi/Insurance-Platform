import { useEffect, useMemo, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Icon } from '../../components/Icon';
import { SectionTitle } from '../../components/Modal';
import { Blank, Panel, PanelHeader, SkeletonRows } from '../../components/ui/Premium';
import { formatNumber, rangeBounds } from '../../utils/date';
import { hasPermission } from '../../utils/access';
import type {
  AnalyticsOverview,
  CompanyOption,
  Complaint,
  ComplaintListResponse,
  DateRange,
  InsuranceType,
  StaffAccess,
} from '../../types';

type ChannelDay = { date: string; whatsapp: number; calls: number };

interface OverviewPageProps {
  access: StaffAccess;
  range: DateRange;
  companies: CompanyOption[];
  onCompany: (companyId: string) => void;
  onInsuranceType: (value: InsuranceType | '') => void;
  companyId: string;
  insuranceType: InsuranceType | '';
  onOpenComplaints: () => void;
  onOpenTicket?: (id: string) => void;
  onOpenChannels?: () => void;
  onOpenAudit?: () => void;
}

const STATUS_LABELS: Record<string, string> = {
  new: 'جديد',
  assigned: 'مُسند',
  in_review: 'قيد المعالجة',
  resolved: 'تم الرد',
  reopened: 'أُعيد فتحه',
  transferred: 'محوّل',
};
const STATUS_COLORS: Record<string, string> = {
  new: '#4F46E5',
  assigned: '#F59E0B',
  in_review: '#F59E0B',
  resolved: '#10B981',
  reopened: '#EF4444',
  transferred: '#64748B',
};
const STATUS_ORDER = ['new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'];

function sevenDayBounds(): { date_from: string; date_to: string } {
  const now = new Date();
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 6));
  return { date_from: start.toISOString(), date_to: now.toISOString() };
}

function sevenDayRows(rows: ChannelDay[]): Array<ChannelDay & { label: string }> {
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const now = new Date();
  return Array.from({ length: 7 }, (_, offset) => {
    const day = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 6 + offset));
    const key = day.toISOString().slice(0, 10);
    const source = byDate.get(key);
    return {
      date: key,
      label: new Intl.DateTimeFormat('ar-EG', { weekday: 'short', timeZone: 'UTC' }).format(day),
      whatsapp: source?.whatsapp ?? 0,
      calls: source?.calls ?? 0,
    };
  });
}

function RequestsTable({ onOpenTicket }: { onOpenTicket?: (id: string) => void }) {
  const [items, setItems] = useState<Complaint[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    void api<ComplaintListResponse>(`/api/staff/complaints${queryString({ page: 1, page_size: 5, sort_by: 'created_at', sort_dir: 'desc' })}`)
      .then((result) => {
        if (active) {
          setItems(result.items ?? []);
          setFailed(false);
        }
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => { active = false; };
  }, []);

  return (
    <Panel className="broker-dashboard-table">
      <PanelHeader
        title="أحدث الطلبات"
        subtitle="آخر الطلبات الواردة ضمن نطاق صلاحيتك"
        actions={<span className="broker-table-caption">{items ? `عرض ${formatNumber(items.length)} من آخر الطلبات` : 'جارٍ التحميل'}</span>}
      />
      {failed ? <Blank title="تعذر تحميل الطلبات" body="تأكد من اتصالك ثم أعد المحاولة." /> : items === null ? <SkeletonRows rows={5} /> : items.length === 0 ? <Blank icon={<Icon name="inbox" size={21} />} title="لا توجد طلبات حديثة" body="ستظهر الطلبات الجديدة هنا بعد وصولها من القنوات المرتبطة." /> : (
        <div className="broker-table-scroll">
          <table className="broker-request-table">
            <thead><tr><th>رقم الطلب</th><th>الموظف</th><th>الشركة</th><th>التأمين</th><th>القناة</th><th>الحالة</th></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td><button type="button" className="broker-ticket-link" onClick={() => onOpenTicket?.(item.id)} disabled={!onOpenTicket}>{item.ticket_no}</button></td>
                  <td>{item.customer_name || '—'}</td>
                  <td>{item.company_name || '—'}</td>
                  <td>{item.insurance_type === 'health' ? 'طبي' : item.insurance_type === 'general' ? 'عام' : 'مشترك'}</td>
                  <td><span className="broker-channel-label">{item.channel === 'whatsapp' ? 'واتساب' : ['phone', 'ai_voice'].includes(item.channel) ? 'مكالمة' : item.channel === 'email' ? 'بريد إلكتروني' : item.channel}</span></td>
                  <td><span className="broker-request-status" data-status={item.status}>{STATUS_LABELS[item.status] ?? item.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function OverviewPage({
  access,
  range,
  companies,
  onCompany,
  onInsuranceType,
  companyId,
  insuranceType,
  onOpenComplaints,
  onOpenTicket,
}: OverviewPageProps) {
  const [report, setReport] = useState<AnalyticsOverview | null>(null);
  const [channels, setChannels] = useState<ChannelDay[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const bounds = useMemo(() => rangeBounds(range), [range]);
  const filterQuery = useMemo(() => queryString({
    ...bounds,
    company_id: companyId,
    insurance_type: insuranceType,
  }), [bounds, companyId, insuranceType]);
  const channelQuery = useMemo(() => queryString({
    ...sevenDayBounds(),
    company_id: companyId,
    insurance_type: insuranceType,
  }), [companyId, insuranceType]);

  useEffect(() => {
    if (!hasPermission(access, 'analytics.view') || !hasPermission(access, 'complaints.view')) return;
    let active = true;
    void Promise.all([
      api<AnalyticsOverview>(`/api/staff/analytics/overview${filterQuery}`),
      api<ChannelDay[]>(`/api/staff/analytics/channels${channelQuery}`),
    ]).then(([overview, series]) => {
      if (active) {
        setReport(overview);
        setChannels(series);
        setError('');
      }
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : 'تعذر تحميل لوحة التحكم.');
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [access, filterQuery, channelQuery]);

  if (!hasPermission(access, 'analytics.view') || !hasPermission(access, 'complaints.view')) {
    return <AccessDenied />;
  }

  const kpis = report?.kpis;
  const total = kpis?.total ?? 0;
  const open = (kpis?.new ?? 0) + (kpis?.assigned ?? 0) + (kpis?.in_review ?? 0) + (kpis?.reopened ?? 0);
  const breaches = kpis?.sla_breaches ?? 0;
  const compliance = total > 0 ? Math.max(0, Math.round(((total - breaches) / total) * 100)) : 0;
  const channelData = sevenDayRows(channels ?? []);
  const statuses = STATUS_ORDER
    .map((status) => ({ status, count: report?.by_status.find((entry) => entry.status === status)?.count ?? 0 }))
    .filter((entry) => entry.count > 0);
  const maxStatus = Math.max(...statuses.map((entry) => entry.count), 1);
  const metrics = [
    { label: 'إجمالي الطلبات', value: total, icon: 'inbox' as const, tone: 'indigo', hint: 'خلال الفترة المحددة' },
    { label: 'قيد المعالجة', value: open, icon: 'clock' as const, tone: 'amber', hint: 'طلبات مفتوحة أو قيد الإسناد' },
    { label: 'الالتزام باتفاقية SLA', value: `${formatNumber(compliance)}٪`, icon: 'shield' as const, tone: 'green', hint: `${formatNumber(breaches)} طلب متجاوز` },
    { label: 'تمت معالجتها', value: kpis?.resolved ?? 0, icon: 'check' as const, tone: 'teal', hint: 'طلبات أُغلقت خلال الفترة' },
  ];

  return (
    <div className="page-stack broker-dashboard">
      <SectionTitle
        eyebrow="Broker Hub · وسيط"
        title="لوحة التحكم"
        description="نظرة سريعة على الطلبات الواردة ومستوى المعالجة."
        action={<button className="button button-primary" type="button" onClick={onOpenComplaints}><Icon name="plus" size={16} /> طلب جديد</button>}
      />

      <div className="broker-dashboard-filters surface-card">
        <div className="broker-filter-intro"><span className="broker-filter-icon"><Icon name="filter" size={16} /></span><div><strong>نطاق البيانات</strong><small>الأرقام ضمن صلاحياتك الحالية</small></div></div>
        <label className="compact-field"><span>شركة التأمين</span><select aria-label="فلتر شركة التأمين" value={companyId} onChange={(event) => onCompany(event.target.value)}><option value="">كل الشركات ضمن نطاقي</option>{companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar || company.name_en}</option>)}</select></label>
        <label className="compact-field"><span>نوع التأمين</span><select aria-label="فلتر نوع التأمين" value={insuranceType} onChange={(event) => onInsuranceType(event.target.value as InsuranceType | '')}><option value="">جميع الأنواع</option>{(access.insurance_types.length === 0 || access.insurance_types.includes('health')) && <option value="health">طبي</option>}{(access.insurance_types.length === 0 || access.insurance_types.includes('general')) && <option value="general">عام</option>}{(access.insurance_types.length === 0 || access.insurance_types.includes('both')) && <option value="both">مشترك</option>}</select></label>
        <span className="broker-live-indicator"><i /> تحديث مباشر من النظام</span>
      </div>

      {error && <div className="inline-alert error" role="alert">{error}</div>}

      <div className="broker-kpi-grid" aria-busy={loading}>
        {metrics.map((metric) => (
          <article className="broker-kpi-card" data-tone={metric.tone} key={metric.label}>
            <span className="broker-kpi-icon"><Icon name={metric.icon} size={18} /></span>
            <div className="broker-kpi-title">{metric.label}</div>
            <strong className="broker-kpi-value">{loading && !report ? '—' : typeof metric.value === 'number' ? formatNumber(metric.value) : metric.value}</strong>
            <span className="broker-kpi-hint">{metric.hint}</span>
          </article>
        ))}
      </div>

      <div className="broker-dashboard-analytics">
        <Panel className="broker-channel-card">
          <PanelHeader title="الطلبات حسب القناة — آخر ٧ أيام" subtitle="مقارنة يومية لطلبات واتساب والمكالمات" actions={<div className="broker-chart-legend"><span><i data-color="whatsapp" /> واتساب</span><span><i data-color="calls" /> مكالمات</span></div>} />
          {loading && channels === null ? <SkeletonRows rows={4} /> : (
            <div className="broker-channel-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={channelData} margin={{ top: 10, right: 5, left: -20, bottom: 0 }} barGap={8}>
                  <CartesianGrid vertical={false} stroke="var(--border-subtle)" strokeDasharray="3 5" />
                  <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                  <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                  <Tooltip cursor={{ fill: 'var(--surface-muted)' }} contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                  <Bar dataKey="whatsapp" name="واتساب" fill="#4F46E5" radius={[5, 5, 0, 0]} barSize={17} />
                  <Bar dataKey="calls" name="مكالمات" fill="#2DD4BF" radius={[5, 5, 0, 0]} barSize={17} />
                </BarChart>
              </ResponsiveContainer>
              {channels?.length === 0 && <span className="broker-chart-empty">لا توجد طلبات بهذه القنوات خلال الفترة.</span>}
            </div>
          )}
        </Panel>

        <Panel className="broker-status-card">
          <PanelHeader title="حالة الطلبات" subtitle="التوزيع حسب الحالة الحالية" />
          {loading && !report ? <SkeletonRows rows={4} /> : statuses.length === 0 ? <Blank title="لا توجد بيانات حالة" /> : (
            <ul className="broker-status-list">
              {statuses.map(({ status, count }) => (
                <li key={status}>
                  <div className="broker-status-meta"><span>{STATUS_LABELS[status] ?? status}</span><strong>{formatNumber(count)}</strong></div>
                  <span className="broker-status-track"><span style={{ width: `${Math.max(3, (count / maxStatus) * 100)}%`, background: STATUS_COLORS[status] }} /></span>
                </li>
              ))}
            </ul>
          )}
          <button className="broker-status-link" type="button" onClick={onOpenComplaints}>عرض صندوق الطلبات <Icon name="arrow" size={15} /></button>
        </Panel>
      </div>

      <RequestsTable onOpenTicket={onOpenTicket} />
    </div>
  );
}
