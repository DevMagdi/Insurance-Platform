import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api, apiResponse, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { SectionTitle } from '../../components/Modal';
import { formatDateTime, formatNumber, rangeBounds } from '../../utils/date';
import type { AccessChange, AuditEvent, DateRange, StaffAccess } from '../../types';

interface AuditPageProps { access: StaffAccess; range: DateRange; }
type AuditTab = 'events' | 'access';
interface AuditFilters {
  actor_id: string;
  complaint_id: string;
  target_id: string;
  action: string;
}
const EMPTY_FILTERS: AuditFilters = { actor_id: '', complaint_id: '', target_id: '', action: '' };
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function download(blob: Blob, filename: string) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
}

const ACTION_LABELS: Record<string, string> = {
  complaint_created: 'إنشاء شكوى', complaint_updated: 'تعديل شكوى', status_changed: 'تغيير الحالة',
  assigned: 'إسناد', reassigned: 'إعادة إسناد', public_reply: 'رد علني', internal_note: 'ملاحظة داخلية',
  escalated: 'تحويل إلى شركة التأمين', complaint_merged: 'دمج شكوى', access_updated: 'تعديل صلاحيات',
  access_replaced: 'استبدال الوصول', user_invited: 'دعوة مستخدم', user_deactivated: 'تعطيل مستخدم',
  team_membership_changed: 'تعديل عضوية فريق', integration_configured: 'حفظ إعداد تكامل',
  integration_disconnected: 'فصل تكامل', report_schedule_saved: 'حفظ تقرير مجدول',
};
const ACTION_OPTIONS = Object.entries(ACTION_LABELS).filter(([action]) => !['access_replaced', 'user_invited', 'user_deactivated', 'team_membership_changed'].includes(action));

export function AuditPage({ access, range }: AuditPageProps) {
  const canSeeAccess = hasPermission(access, 'users.manage_access') && access.all_companies && access.ownership === 'all' && access.insurance_types.length === 0;
  const [tab, setTab] = useState<AuditTab>('events');
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [changes, setChanges] = useState<AccessChange[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [draftFilters, setDraftFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const bounds = useMemo(() => rangeBounds(range), [range]);

  const load = useCallback(async () => {
    if (!hasPermission(access, 'audit.view')) { setLoading(false); return; }
    setLoading(true);
    setError('');
    try {
      if (tab === 'events') {
        const result = await api<AuditEvent[]>(`/api/staff/audit/events${queryString({
          start: bounds.date_from, end: bounds.date_to, limit: 200,
          actor_id: filters.actor_id, complaint_id: filters.complaint_id, action: filters.action,
        })}`);
        setEvents(result);
      } else if (canSeeAccess) {
        const result = await api<AccessChange[]>(`/api/staff/audit/access-changes${queryString({
          start: bounds.date_from, end: bounds.date_to, limit: 200, target_id: filters.target_id,
        })}`);
        setChanges(result);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل سجل التدقيق.');
    } finally {
      setLoading(false);
    }
  }, [access, bounds, canSeeAccess, filters, tab]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  if (!hasPermission(access, 'audit.view')) return <AccessDenied title="سجل التدقيق غير متاح" />;

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const draft = {
      actor_id: draftFilters.actor_id.trim(),
      complaint_id: draftFilters.complaint_id.trim(),
      target_id: draftFilters.target_id.trim(),
      action: draftFilters.action,
    };
    const idFields = tab === 'events'
      ? [['معرّف الموظف', draft.actor_id], ['معرّف الشكوى', draft.complaint_id]] as const
      : [['معرّف المستخدم المستهدف', draft.target_id]] as const;
    const invalid = idFields.find(([, value]) => value !== '' && !UUID_PATTERN.test(value));
    if (invalid) {
      setError(`أدخل ${invalid[0]} بصيغة UUID صحيحة.`);
      return;
    }
    setError('');
    setFilters(draft);
  };

  const exportCurrent = async (format: 'csv' | 'xlsx' = 'csv') => {
    try {
      const extension = tab === 'events' ? format : 'csv';
      const url = tab === 'events'
        ? `/api/staff/audit/events.${extension}${queryString({
          start: bounds.date_from, end: bounds.date_to, actor_id: filters.actor_id,
          complaint_id: filters.complaint_id, action: filters.action,
        })}`
        : `/api/staff/audit/access-changes.csv${queryString({ start: bounds.date_from, end: bounds.date_to, target_id: filters.target_id })}`;
      const response = await apiResponse(url);
      download(await response.blob(), tab === 'events' ? `audit-events-${range.from}-${range.to}.${extension}` : `access-changes-${range.from}-${range.to}.csv`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تصدير سجل التدقيق.');
    }
  };

  const count = tab === 'events' ? events.length : changes.length;
  const exportAllowed = tab === 'events' ? hasPermission(access, 'audit.view') : canSeeAccess;

  return <div className="page-stack">
    <SectionTitle eyebrow="الامتثال والحوكمة" title="سجل التدقيق" description="أحداث الوصول والإجراءات التشغيلية ضمن النطاق المصرّح، مع سجل منفصل لتغييرات الصلاحيات." action={exportAllowed ? <div className="audit-export-actions"><button className="button button-light" type="button" onClick={() => { void exportCurrent('csv'); }}><Icon name="download" size={15} /> تصدير CSV</button>{tab === 'events' && <button className="button button-light" type="button" onClick={() => { void exportCurrent('xlsx'); }}><Icon name="download" size={15} /> تصدير XLSX</button>}</div> : undefined} />
    <div className="operation-tabs audit-tabs" role="tablist" aria-label="أنواع سجل التدقيق">
      <button role="tab" aria-selected={tab === 'events'} className={tab === 'events' ? 'active' : ''} type="button" onClick={() => setTab('events')}><Icon name="audit" size={15} /> الأحداث</button>
      {canSeeAccess && <button role="tab" aria-selected={tab === 'access'} className={tab === 'access' ? 'active' : ''} type="button" onClick={() => setTab('access')}><Icon name="shield" size={15} /> تغييرات الوصول</button>}
    </div>
    <form className="audit-filter-form surface-card" onSubmit={applyFilters}>
      {tab === 'events' ? <>
        <label className="compact-field"><span>الإجراء</span><select aria-label="تصفية حسب الإجراء" value={draftFilters.action} onChange={(event) => setDraftFilters((current) => ({ ...current, action: event.target.value }))}><option value="">كل الإجراءات</option>{ACTION_OPTIONS.map(([action, label]) => <option value={action} key={action}>{label}</option>)}</select></label>
        <label className="compact-field"><span>معرّف الموظف</span><input aria-label="تصفية حسب معرّف الموظف" value={draftFilters.actor_id} onChange={(event) => setDraftFilters((current) => ({ ...current, actor_id: event.target.value }))} placeholder="UUID" dir="ltr" /></label>
        <label className="compact-field"><span>معرّف الشكوى</span><input aria-label="تصفية حسب معرّف الشكوى" value={draftFilters.complaint_id} onChange={(event) => setDraftFilters((current) => ({ ...current, complaint_id: event.target.value }))} placeholder="UUID" dir="ltr" /></label>
      </> : <label className="compact-field"><span>المستخدم المستهدف</span><input aria-label="تصفية حسب المستخدم المستهدف" value={draftFilters.target_id} onChange={(event) => setDraftFilters((current) => ({ ...current, target_id: event.target.value }))} placeholder="UUID" dir="ltr" /></label>}
      <button className="button button-dark" type="submit"><Icon name="filter" size={15} /> تطبيق الفلاتر</button>
      <button className="button button-quiet" type="button" onClick={() => { setDraftFilters(EMPTY_FILTERS); setFilters(EMPTY_FILTERS); setError(''); }}>مسح</button>
    </form>
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    <section className="surface-card admin-table-card">
      <div className="table-topline"><div><strong>{loading ? 'جارٍ التحميل…' : `${formatNumber(count)} ${tab === 'events' ? 'حدث' : 'تغيير وصول'}`}</strong><span>{tab === 'events' ? `الفترة المحددة · ${range.from} — ${range.to}` : `تغييرات الوصول · ${range.from} — ${range.to}`}</span></div><button className="icon-button small" type="button" onClick={() => { void load(); }} aria-label="تحديث سجل التدقيق" disabled={loading}><Icon name="refresh" size={15} /></button></div>
      <div className="table-wrap"><table className="data-table operations-table">
        <thead>{tab === 'events' ? <tr><th>الوقت</th><th>الإجراء</th><th>نوع المورد</th><th>معرّف المورد</th><th>معرّف الشكوى</th></tr> : <tr><th>الوقت</th><th>المستخدم المستهدف</th><th>نوع التغيير</th><th>التغيير السابق</th><th>التغيير الجديد</th></tr>}</thead>
        <tbody>
          {loading && <tr><td colSpan={5}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل السجل…</div></td></tr>}
          {!loading && tab === 'events' && events.map((event) => <tr key={event.id}><td className="date-cell">{formatDateTime(event.created_at)}</td><td><span className="audit-action"><i />{ACTION_LABELS[event.action] ?? event.action}</span></td><td>{event.resource_type || '—'}</td><td><code className="mono-id">{event.resource_id ?? '—'}</code></td><td><code className="mono-id">{event.complaint_id ?? '—'}</code></td></tr>)}
          {!loading && tab === 'access' && changes.map((change) => <tr key={change.id}><td className="date-cell">{formatDateTime(change.created_at)}</td><td><code className="mono-id">{change.target_user_id}</code></td><td>{ACTION_LABELS[change.change_type] ?? change.change_type}</td><td><code className="state-preview">{JSON.stringify(change.before_state).slice(0, 100)}</code></td><td><code className="state-preview">{JSON.stringify(change.after_state).slice(0, 100)}</code></td></tr>)}
          {!loading && count === 0 && <tr><td colSpan={5}><div className="empty-table"><span className="empty-icon"><Icon name="audit" size={20} /></span><strong>لا توجد أحداث في الفترة المحددة</strong><span>جرّب فترة زمنية أخرى أو غيّر الفلاتر.</span></div></td></tr>}
        </tbody>
      </table></div>
    </section>
  </div>;
}
