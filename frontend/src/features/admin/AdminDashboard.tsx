import { useCallback, useEffect, useState } from 'react';
import { api, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Icon, type IconName } from '../../components/Icon';
import { hasPermission } from '../../utils/access';
import { displayRange, formatNumber, rangeBounds } from '../../utils/date';
import type { AnalyticsOverview, AppRoute, DateRange, StaffAccess, StaffDirectoryEntry } from '../../types';
import { canViewAdminDashboard } from './adminAccess';
import './admin-dashboard.css';

const STATUS: Record<string, string> = {
  new: 'جديدة', assigned: 'مُسندة', in_review: 'قيد المراجعة', resolved: 'تم الحل',
  reopened: 'أُعيد فتحها', transferred: 'محوّلة',
};

function Trend({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  const points = values.map((value, index) => `${values.length === 1 ? 50 : index * 100 / (values.length - 1)},${62 - value * 52 / max}`).join(' ');
  return <div className="admin-trend" role="img" aria-label={`الشكاوى الواردة: ${values.map((value) => formatNumber(value)).join('، ') || 'لا توجد بيانات'}`}>
    {values.length ? <svg viewBox="0 0 100 70" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <line x1="0" y1="62" x2="100" y2="62" className="trend-grid" />
      <line x1="0" y1="36" x2="100" y2="36" className="trend-grid" />
      <polyline points={`0,62 ${points} 100,62`} className="trend-fill" />
      <polyline points={points} className="trend-line" />
    </svg> : <p className="admin-empty">لا توجد بيانات للفترة المحددة.</p>}
  </div>;
}

interface Props {
  access: StaffAccess;
  range: DateRange;
  onNavigate: (route: AppRoute) => void;
}

export function AdminDashboard({ access, range, onNavigate }: Props) {
  const [report, setReport] = useState<AnalyticsOverview | null>(null);
  const [users, setUsers] = useState<StaffDirectoryEntry[] | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [updated, setUpdated] = useState<Date | null>(null);
  const allowed = canViewAdminDashboard(access);
  const canSeeUsers = hasPermission(access, 'users.view');
  const { date_from, date_to } = rangeBounds(range);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!allowed) return;
    setLoading(true);
    try {
      const [overview, directory] = await Promise.all([
        api<AnalyticsOverview>(`/api/staff/analytics/overview${queryString({ date_from, date_to })}`, { signal }),
        canSeeUsers ? api<StaffDirectoryEntry[]>('/api/staff/users', { signal }).catch(() => null) : Promise.resolve(null),
      ]);
      if (signal?.aborted) return;
      setReport(overview);
      setUsers(directory);
      setUpdated(new Date());
      setError('');
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : 'تعذر تحميل لوحة الإدارة.');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [allowed, canSeeUsers, date_from, date_to]);

  useEffect(() => {
    const controller = new AbortController();
    queueMicrotask(() => { if (!controller.signal.aborted) void load(controller.signal); });
    const interval = window.setInterval(() => { void load(controller.signal); }, 60_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [load]);

  if (!allowed) return <AccessDenied title="لوحة الإدارة غير متاحة" />;

  const kpis = report?.kpis;
  const cards: Array<{ label: string; value: string; note: string; icon: IconName; tone: string }> = [
    { label: 'إجمالي الشكاوى', value: formatNumber(kpis?.total), note: 'خلال الفترة المختارة', icon: 'inbox', tone: 'gold' },
    { label: 'بانتظار المتابعة', value: formatNumber(kpis ? kpis.new + kpis.reopened : undefined), note: 'جديدة أو أُعيد فتحها', icon: 'clock', tone: 'blue' },
    { label: 'تجاوزات مستوى الخدمة', value: formatNumber(kpis?.sla_breaches), note: 'تحتاج إلى انتباه', icon: 'shield', tone: 'red' },
    { label: 'تم حلها', value: formatNumber(kpis?.resolved), note: 'ضمن الفترة', icon: 'check', tone: 'green' },
  ];
  const shortcuts: Array<{ page: AppRoute['page']; label: string; description: string; icon: IconName; visible: boolean }> = [
    { page: 'complaints', label: 'قائمة الشكاوى', description: 'مراجعة الحالات وتوزيع العمل', icon: 'inbox', visible: true },
    { page: 'users', label: 'المستخدمون', description: 'الحسابات وسياسات الوصول', icon: 'users', visible: canSeeUsers },
    { page: 'roles', label: 'الأدوار والصلاحيات', description: 'ضبط صلاحيات الفريق', icon: 'roles', visible: hasPermission(access, 'roles.manage') },
    { page: 'audit', label: 'سجل التدقيق', description: 'متابعة التغييرات والأنشطة', icon: 'audit', visible: hasPermission(access, 'audit.view') },
    { page: 'storage', label: 'مركز التخزين', description: 'استعراض بيانات الملفات', icon: 'layers', visible: hasPermission(access, 'storage.manage') },
  ];
  const top = report?.by_company.slice(0, 5) ?? [];
  const maxCompany = Math.max(1, ...top.map((entry) => entry.count));
  const maxStatus = Math.max(1, ...(report?.by_status.map((entry) => entry.count) ?? []));

  return <div className="admin-dashboard page-stack">
    <header className="admin-hero">
      <div><span className="admin-kicker"><span className="admin-kicker-dot" /> مركز القيادة · نطاق الإدارة العام</span>
        <h1>لوحة الإدارة</h1><p>متابعة الشكاوى ومستوى الخدمة وإدارة الفريق من مكان واحد.</p>
        <span className="admin-period">الفترة: {displayRange(range)}</span>
      </div>
      <button className="button button-quiet" type="button" onClick={() => { void load(); }} disabled={loading}>
        <Icon name="refresh" size={16} /> تحديث البيانات
      </button>
    </header>

    {error && <div className="inline-alert error" role="alert">{error} <button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    {loading && !report && <p role="status" className="admin-empty">جارٍ تحميل مؤشرات الإدارة…</p>}
    {updated && <p className="admin-updated">آخر تحديث: {updated.toLocaleTimeString('ar-EG', { hour: '2-digit', minute: '2-digit' })}{loading ? ' · جارٍ التحديث…' : ''}</p>}

    <div className="admin-kpis">{cards.map((card) => <article className={`admin-kpi admin-kpi-${card.tone}`} key={card.label}>
      <div className="admin-kpi-head"><span>{card.label}</span><Icon name={card.icon} size={19} /></div>
      <strong>{card.value}</strong><small>{card.note}</small>
    </article>)}</div>

    <div className="admin-data-grid">
      <section className="admin-panel admin-trend-panel"><div className="admin-panel-head"><div><span className="admin-eyebrow">حركة الشكاوى</span><h2>الوارد خلال الفترة</h2></div><span className="admin-panel-number">{formatNumber(kpis?.total)}</span></div>
        <Trend values={report?.time_series.map((item) => item.arrived) ?? []} />
        {report?.time_series.length ? <div className="admin-trend-labels"><span>{report.time_series[0].date}</span><span>{report.time_series.at(-1)?.date}</span></div> : null}
      </section>
      <section className="admin-panel"><div className="admin-panel-head"><div><span className="admin-eyebrow">سير المعالجة</span><h2>توزيع الحالات</h2></div></div>
        {report?.by_status.length ? <div className="admin-bars">{report.by_status.map((entry) => <div className="admin-bar-row" key={entry.status}>
          <span>{STATUS[entry.status] ?? entry.status}</span><div className="admin-bar-track"><span style={{ width: `${entry.count / maxStatus * 100}%` }} /></div><strong>{formatNumber(entry.count)}</strong>
        </div>)}</div> : <p className="admin-empty">لا توجد حالات للفترة المحددة.</p>}
      </section>
    </div>

    <div className="admin-data-grid">
      <section className="admin-panel"><div className="admin-panel-head"><div><span className="admin-eyebrow">نظرة حسب الشركة</span><h2>أعلى الشركات وروداً</h2></div></div>
        {top.length ? <div className="admin-bars">{top.map((entry) => <div className="admin-bar-row" key={entry.company_id}>
          <span title={entry.name}>{entry.name}</span><div className="admin-bar-track company"><span style={{ width: `${entry.count / maxCompany * 100}%` }} /></div><strong>{formatNumber(entry.count)}</strong>
        </div>)}</div> : <p className="admin-empty">لا توجد بيانات للفترة المحددة.</p>}
      </section>
      <section className="admin-panel"><div className="admin-panel-head"><div><span className="admin-eyebrow">إدارة الفريق</span><h2>الحسابات والوصول</h2></div></div>
        {canSeeUsers ? <>{users === null && !loading && <p className="admin-empty">تعذر تحميل بيانات الفريق حالياً.</p>}<div className="admin-team-stats"><div><strong>{formatNumber(users?.filter((user) => user.status === 'active').length)}</strong><span>حساب مفعّل</span></div><div><strong>{formatNumber(users?.filter((user) => user.status === 'invited').length)}</strong><span>بانتظار الدعوة</span></div><div><strong>{formatNumber(users?.filter((user) => user.two_factor_required).length)}</strong><span>تتطلب المصادقة الثنائية</span></div></div></> : <p className="admin-empty">بيانات الفريق متاحة لمن لديه صلاحية عرض المستخدمين.</p>}
        {canSeeUsers && <button type="button" className="admin-text-link" onClick={() => onNavigate({ page: 'users' })}>إدارة المستخدمين ←</button>}
      </section>
    </div>
    <section className="admin-panel"><div className="admin-panel-head"><div><span className="admin-eyebrow">اختصارات العمل</span><h2>انتقل إلى الإدارة</h2></div></div>
      <div className="admin-shortcuts">{shortcuts.filter((item) => item.visible).map((item) => <button key={item.page} type="button" onClick={() => onNavigate({ page: item.page } as AppRoute)}>
        <span className="admin-shortcut-icon"><Icon name={item.icon} size={19} /></span><span><strong>{item.label}</strong><small>{item.description}</small></span><Icon name="arrow" size={16} />
      </button>)}</div>
    </section>
  </div>;
}
