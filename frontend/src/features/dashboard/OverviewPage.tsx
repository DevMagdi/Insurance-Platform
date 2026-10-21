import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { SectionTitle } from '../../components/Modal';
import { formatNumber, rangeBounds } from '../../utils/date';
import type {
  AnalyticsOverview,
  CompanyOption,
  DateRange,
  InsuranceType,
  StaffAccess,
} from '../../types';

const PALETTE = ['var(--gold-500)', 'var(--emerald-500)', 'var(--info)', 'var(--fuchsia)', 'var(--orange)', 'var(--ink-400)'];
/** Chart colors speak the same language as status chips (foundation.css). */
const STATUS_COLORS: Record<string, string> = {
  new: 'var(--info)',
  assigned: 'var(--violet)',
  in_review: 'var(--warning)',
  resolved: 'var(--success)',
  reopened: 'var(--orange)',
  transferred: 'var(--fuchsia)',
};
const STATUS_NAMES: Record<string, string> = {
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم الحل',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
};
const TYPE_NAMES: Record<InsuranceType, string> = {
  health: 'طبي',
  general: 'عام',
  both: 'مشترك',
};
const WEEKDAYS = ['الأحد', 'الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'];

interface OverviewPageProps {
  access: StaffAccess;
  range: DateRange;
  companies: CompanyOption[];
  onCompany: (companyId: string) => void;
  onInsuranceType: (value: InsuranceType | '') => void;
  companyId: string;
  insuranceType: InsuranceType | '';
  onOpenComplaints: () => void;
}

function MetricCard({ label, value, detail, icon, tone }: {
  label: string;
  value: string;
  detail: string;
  icon: 'inbox' | 'clock' | 'shield' | 'chart' | 'check' | 'message' | 'arrow' | 'refresh';
  tone: string;
}) {
  return (
    <article className="metric-card">
      <div className={`metric-icon ${tone}`}><Icon name={icon} size={18} /></div>
      <div className="metric-label-row"><span>{label}</span><span className="metric-dot" /></div>
      <strong className="metric-value">{value}</strong>
      <span className="metric-detail">{detail}</span>
    </article>
  );
}

function ChartCard({ title, subtitle, children, className = '' }: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`surface-card chart-card ${className}`}>
      <div className="chart-card-heading"><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div></div>
      {children}
    </section>
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
}: OverviewPageProps) {
  const [report, setReport] = useState<AnalyticsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState('');
  const bounds = useMemo(() => rangeBounds(range), [range]);
  const filterQuery = useMemo(() => queryString({
    ...bounds,
    company_id: companyId,
    insurance_type: insuranceType,
  }), [bounds, companyId, insuranceType]);

  const refresh = useCallback(async () => {
    if (!hasPermission(access, 'analytics.view') || !hasPermission(access, 'complaints.view')) {
      setLoading(false);
      return;
    }
    try {
      setError('');
      const result = await api<AnalyticsOverview>(`/api/staff/analytics/overview${filterQuery}`);
      setReport(result);
      setLastUpdated(new Date());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل التحليلات.');
    } finally {
      setLoading(false);
    }
  }, [access, filterQuery]);

  useEffect(() => {
    let active = true;
    const run = async () => {
      if (!hasPermission(access, 'analytics.view') || !hasPermission(access, 'complaints.view')) {
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const result = await api<AnalyticsOverview>(`/api/staff/analytics/overview${filterQuery}`);
        if (active) {
          setReport(result);
          setLastUpdated(new Date());
          setError('');
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : 'تعذر تحميل التحليلات.');
      } finally {
        if (active) setLoading(false);
      }
    };
    void run();
    const interval = window.setInterval(() => { void refresh(); }, 30_000);
    const onFocus = () => { void refresh(); };
    window.addEventListener('focus', onFocus);
    return () => {
      active = false;
      window.clearInterval(interval);
      window.removeEventListener('focus', onFocus);
    };
  }, [access, filterQuery, refresh]);

  if (!hasPermission(access, 'analytics.view') || !hasPermission(access, 'complaints.view')) {
    return <AccessDenied title="لوحة المؤشرات غير متاحة" />;
  }

  const kpis = report?.kpis;
  const metrics = [
    { label: 'إجمالي الطلبات', value: formatNumber(kpis?.total), detail: 'وصلت خلال الفترة المختارة', icon: 'inbox' as const, tone: 'metric-gold' },
    { label: 'جديدة', value: formatNumber(kpis?.new), detail: 'بانتظار المعالجة', icon: 'message' as const, tone: 'metric-blue' },
    { label: 'مُسندة', value: formatNumber(kpis?.assigned), detail: 'لدى فريق مختص', icon: 'check' as const, tone: 'metric-teal' },
    { label: 'قيد المراجعة', value: formatNumber(kpis?.in_review), detail: 'قيد المتابعة الآن', icon: 'clock' as const, tone: 'metric-violet' },
    { label: 'تم الحل', value: formatNumber(kpis?.resolved), detail: 'أُغلقت خلال الفترة', icon: 'check' as const, tone: 'metric-green' },
    { label: 'محوّلة للشركة', value: formatNumber(kpis?.transferred), detail: 'أُحيلت إلى شركة التأمين', icon: 'arrow' as const, tone: 'metric-orange' },
    { label: 'متوسط زمن الحل', value: kpis?.avg_resolution_hours == null ? '—' : `${formatNumber(kpis.avg_resolution_hours, 1)} س`, detail: 'من الفتح حتى الحل', icon: 'clock' as const, tone: 'metric-blue' },
    { label: 'تجاوزات SLA', value: formatNumber(kpis?.sla_breaches), detail: 'تحتاج إلى انتباه', icon: 'shield' as const, tone: 'metric-red' },
    { label: 'معدل إعادة الفتح', value: kpis ? `${formatNumber(kpis.reopen_rate * 100, 1)}٪` : '—', detail: `${formatNumber(kpis?.reopened)} طلب أُعيد فتحها`, icon: 'refresh' as const, tone: 'metric-purple' },
    { label: 'رضا العملاء CSAT', value: kpis?.csat_avg == null ? '—' : `${formatNumber(kpis.csat_avg, 1)} / 5`, detail: 'متوسط التقييمات المستلمة', icon: 'chart' as const, tone: 'metric-teal' },
  ];

  const byType = report?.by_type.map((entry) => ({ ...entry, label: TYPE_NAMES[entry.insurance_type] })) ?? [];
  const byStatus = report?.by_status.map((entry) => ({ ...entry, label: STATUS_NAMES[entry.status] ?? entry.status })) ?? [];
  const byAgent = report?.by_agent.map((entry) => ({
    name: entry.name || (entry.agent_id ? `موظف ${entry.agent_id.slice(0, 6)}` : 'غير مُسند'),
    count: entry.count,
  })) ?? [];
  const resolutionData = [
    { name: 'أقل من ٢٤ ساعة', value: report?.resolution_hours.under_24h ?? 0 },
    { name: '١–٣ أيام', value: report?.resolution_hours['24_to_72h'] ?? 0 },
    { name: '٣–٧ أيام', value: report?.resolution_hours['72_to_168h'] ?? 0 },
    { name: 'أكثر من أسبوع', value: report?.resolution_hours.over_168h ?? 0 },
  ];
  const heatmapValues = (report?.heatmap ?? []).map((cell) => cell.count);
  const maxHeat = Math.max(...heatmapValues, 1);
  const heatByHour = new Map((report?.heatmap ?? []).map((item) => [`${item.weekday}-${item.hour}`, item.count]));

  return (
    <div className="page-stack">
      <SectionTitle
        eyebrow="مساحة التحليلات"
        title="نظرة عامة على الأداء"
        description="ملخص تشغيلي موحّد للطلبات، جودة المعالجة، ومستوى الخدمة."
        action={(
          <div className="live-status"><span className="scope-live-dot" />
            <span>تحديث مباشر كل ٣٠ ثانية</span>
            {lastUpdated && <small>آخر تحديث {lastUpdated.toLocaleTimeString('ar-EG', { hour: '2-digit', minute: '2-digit' })}</small>}
          </div>
        )}
      />

      <div className="filter-strip surface-card">
        <div className="filter-strip-label"><span className="filter-strip-icon"><Icon name="filter" size={16} /></span><div><strong>تصفية المؤشرات</strong><small>النطاق الزمني موحّد لكل الرسوم</small></div></div>
        <label className="compact-field"><span>شركة التأمين</span>
          <select aria-label="فلتر شركة التأمين" value={companyId} onChange={(event) => onCompany(event.target.value)}>
            <option value="">كل الشركات ضمن نطاقي</option>
            {companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar || company.name_en}</option>)}
          </select>
        </label>
        <label className="compact-field"><span>نوع التأمين</span>
          <select aria-label="فلتر نوع التأمين" value={insuranceType} onChange={(event) => onInsuranceType(event.target.value as InsuranceType | '')}>
            <option value="">جميع الأنواع</option>
            {access.insurance_types.length === 0 || access.insurance_types.includes('health') ? <option value="health">طبي</option> : null}
            {access.insurance_types.length === 0 || access.insurance_types.includes('general') ? <option value="general">عام</option> : null}
            {access.insurance_types.length === 0 || access.insurance_types.includes('both') ? <option value="both">مشترك</option> : null}
          </select>
        </label>
        <button className="button button-quiet" type="button" onClick={() => { void refresh(); }} disabled={loading}>
          <Icon name="refresh" size={15} /> تحديث
        </button>
      </div>

      {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void refresh(); }}>إعادة المحاولة</button></div>}

      <div className="metric-grid">
        {metrics.map((metric) => <MetricCard key={metric.label} {...metric} />)}
      </div>
      <div className="arrival-summary">
        <span className="arrival-summary-mark"><Icon name="calendar" size={16} /></span>
        <span>وصلت <strong>{formatNumber(kpis?.total)}</strong> طلب في النطاق الزمني المحدد.</span>
        <button type="button" onClick={onOpenComplaints}>عرض قائمة الطلبات <Icon name="arrow" size={15} /></button>
      </div>

      {loading && !report ? (
        <div className="ov-loading" role="status" aria-live="polite" aria-label="جارٍ تجهيز التحليلات حسب نطاق صلاحيتك…">
          <div className="charts-grid charts-top" aria-hidden="true">
            <div className="skeleton ov-skel-chart chart-wide" />
            <div className="skeleton ov-skel-chart" />
          </div>
          <div className="charts-grid" aria-hidden="true">
            <div className="skeleton ov-skel-chart" />
            <div className="skeleton ov-skel-chart" />
            <div className="skeleton ov-skel-chart" />
          </div>
        </div>
      ) : report ? (
        <>
          <div className="charts-grid charts-top">
            <ChartCard title="اتجاه وصول الطلبات" subtitle="عدد الطلبات الجديدة يومياً خلال الفترة" className="chart-wide">
              <div className="chart-container trend-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={report.time_series} margin={{ top: 10, right: 8, left: -16, bottom: 0 }}>
                    <defs><linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--emerald-500)" stopOpacity={0.24} /><stop offset="100%" stopColor="var(--emerald-500)" stopOpacity={0.01} /></linearGradient></defs>
                    <CartesianGrid strokeDasharray="3 5" vertical={false} stroke="var(--chart-grid)" />
                    <XAxis dataKey="date" tickFormatter={(value: string) => value.slice(5)} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                    <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                    <Tooltip contentStyle={{ borderRadius: 14, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} labelFormatter={(value) => String(value)} />
                    <Area type="monotone" dataKey="arrived" name="الطلبات" stroke="var(--emerald-500)" strokeWidth={2.6} fill="url(#trendFill)" activeDot={{ r: 5, fill: 'var(--gold-500)', stroke: 'var(--surface)', strokeWidth: 2 }} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
            <ChartCard title="حسب الحالة" subtitle="صورة لحظية للحالات المطابقة للفترة">
              <div className="chart-container compact-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={byStatus} layout="vertical" margin={{ top: 4, right: 0, left: 0, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 5" horizontal={false} stroke="var(--chart-grid)" />
                    <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <YAxis type="category" dataKey="label" width={82} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                    <Bar dataKey="count" name="الطلبات" radius={[0, 6, 6, 0]} barSize={12}>
                      {byStatus.map((entry, index) => <Cell key={entry.status} fill={STATUS_COLORS[entry.status] ?? PALETTE[index % PALETTE.length]} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          </div>

          <div className="charts-grid">
            <ChartCard title="الشركات الأعلى استقبالاً" subtitle="مقارنة بعدد الطلبات ضمن النطاق">
              <div className="chart-container compact-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={report.by_company.slice(0, 6)} margin={{ top: 8, right: 2, left: -20, bottom: 26 }}>
                    <CartesianGrid strokeDasharray="3 5" vertical={false} stroke="var(--chart-grid)" />
                    <XAxis dataKey="name" interval={0} angle={-16} textAnchor="end" tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                    <Bar dataKey="count" name="الطلبات" fill="var(--gold-500)" radius={[6, 6, 0, 0]} barSize={24} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
            <ChartCard title="حسب نوع التأمين" subtitle="التوزيع بين الطبي والعام والمشترك">
              <div className="type-chart-layout">
                <div className="chart-container type-chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={byType} dataKey="count" nameKey="label" innerRadius={48} outerRadius={72} paddingAngle={4} stroke="none">
                        {byType.map((entry, index) => <Cell key={entry.insurance_type} fill={PALETTE[index % PALETTE.length]} />)}
                      </Pie>
                      <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
                <div className="chart-legend">
                  {byType.map((entry, index) => <div key={entry.insurance_type}><span style={{ background: PALETTE[index % PALETTE.length] }} />{TYPE_NAMES[entry.insurance_type]}<strong>{formatNumber(entry.count)}</strong></div>)}
                  {byType.length === 0 && <span className="muted">لا توجد بيانات في هذه الفترة</span>}
                </div>
              </div>
            </ChartCard>
            <ChartCard title="حجم العمل حسب الموظف" subtitle="عدد الطلبات المسندة ضمن النطاق">
              <div className="chart-container compact-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={byAgent.slice(0, 6)} layout="vertical" margin={{ top: 4, right: 4, left: 4, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 5" horizontal={false} stroke="var(--chart-grid)" />
                    <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <YAxis type="category" dataKey="name" width={105} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                    <Bar dataKey="count" name="الطلبات" fill="var(--emerald-500)" radius={[0, 6, 6, 0]} barSize={13} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
            <ChartCard title="زمن إغلاق الطلبات" subtitle="مدة الحل للطلبات المغلقة">
              <div className="chart-container compact-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={resolutionData} margin={{ top: 8, right: 2, left: -18, bottom: 8 }}>
                    <CartesianGrid strokeDasharray="3 5" vertical={false} stroke="var(--chart-grid)" />
                    <XAxis dataKey="name" interval={0} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: 'var(--text-muted)' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)' }} />
                    <Bar dataKey="value" name="الطلبات" fill="var(--info)" radius={[6, 6, 0, 0]} barSize={26} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          </div>

          <div className="charts-grid charts-bottom">
            <ChartCard title="خريطة أوقات الوصول" subtitle="يوم الأسبوع × ساعة اليوم بتوقيت القاهرة" className="chart-wide">
              <div className="heatmap-wrap" dir="ltr">
                <div className="heatmap-hours"><span />{Array.from({ length: 24 }, (_, hour) => <span key={hour}>{hour % 3 === 0 ? String(hour).padStart(2, '0') : ''}</span>)}</div>
                {WEEKDAYS.map((day, weekday) => (
                  <div className="heatmap-row" key={day}>
                    <span className="heatmap-day" dir="rtl">{day}</span>
                    {Array.from({ length: 24 }, (_, hour) => {
                      const value = heatByHour.get(`${weekday}-${hour}`) ?? 0;
                      const opacity = value === 0 ? 0.06 : 0.18 + (value / maxHeat) * 0.78;
                      return <span className="heatmap-cell" key={hour} title={`${day} · ${hour}:00 — ${formatNumber(value)} طلب`} style={{ backgroundColor: `color-mix(in srgb, var(--emerald-500) ${Math.round(opacity * 100)}%, transparent)` }} />;
                    })}
                  </div>
                ))}
                <div className="heatmap-caption"><span>أقل</span><i /><i /><i /><i /><span>أعلى</span></div>
              </div>
            </ChartCard>
            <ChartCard title="الموضوعات المتكررة" subtitle="التصنيفات الأكثر وروداً">
              <div className="issues-list">
                {report.top_issues.slice(0, 6).map((issue, index) => (
                  <div className="issue-row" key={issue.category}>
                    <span className="issue-rank">{String(index + 1).padStart(2, '0')}</span>
                    <span className="issue-name">{issue.category}</span>
                    <strong>{formatNumber(issue.count)}</strong>
                  </div>
                ))}
                {report.top_issues.length === 0 && <p className="empty-inline">لا توجد تصنيفات لهذه الفترة.</p>}
              </div>
            </ChartCard>
          </div>

          <div className="charts-grid charts-insights">
            <ChartCard title="بطاقات أداء الشركات" subtitle="مقارنة زمن الحل، التجاوزات، ورضا العملاء" className="scorecard-card">
              <div className="table-wrap">
                <table className="data-table score-table">
                  <thead><tr><th>شركة التأمين</th><th>الطلبات</th><th>متوسط الحل</th><th>تجاوزات SLA</th><th>CSAT</th></tr></thead>
                  <tbody>{report.company_scorecards.slice(0, 8).map((company) => (
                    <tr key={company.company_id}><td><span className="company-mini-mark">{company.name.slice(0, 1)}</span><strong>{company.name}</strong></td><td>{formatNumber(company.total)}</td><td>{company.avg_resolution_hours == null ? '—' : `${formatNumber(company.avg_resolution_hours, 1)} س`}</td><td><span className={company.sla_breaches > 0 ? 'score-warning' : 'score-good'}>{formatNumber(company.sla_breaches)}</span></td><td>{company.csat_avg == null ? '—' : `${formatNumber(company.csat_avg, 1)} / 5`}</td></tr>
                  ))}</tbody>
                </table>
              </div>
            </ChartCard>
            <section className="surface-card ai-insight-card">
              <div className="ai-icon"><Icon name="spark" size={19} /></div>
              <p className="eyebrow">مساعد وَثيق</p>
              <h3>ملخص الرؤى الذكية</h3>
              <p>ستظهر هنا ملخصات الاتجاهات والتوصيات بعد ربط موفّر الذكاء الاصطناعي واعتماد مخرجاته.</p>
              <div className="ai-status"><span className="status-dot status-pending" /> موفّر الذكاء الاصطناعي غير مفعّل</div>
              <small>لا يتم إنشاء توصيات تجريبية أو عرض بيانات غير مستندة إلى مصدر.</small>
            </section>
          </div>
        </>
      ) : (
        <div className="loading-surface"><span>لا توجد بيانات تحليلية متاحة لهذا النطاق.</span></div>
      )}
    </div>
  );
}
