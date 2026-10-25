import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent } from 'react';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api, apiResponse, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Modal } from '../../components/Modal';
import { getCsrfToken } from '../../lib/session';
import { hasPermission } from '../../utils/access';
import { formatDateTime, formatNumber } from '../../utils/date';
import type { StaffAccess } from '../../types';
import './crm-module.css';

/**
 * The platform's only CRM workspace (`/staff/crm-module`, `/api/crm`):
 * Dashboard | Pipeline (Kanban) | Leads | Tasks | Import | Performance |
 * History. The sidebar group is permission-gated in StaffConsole with
 * the same `crm.view` check repeated here.
 */
export type CrmModuleTab = 'dashboard' | 'pipeline' | 'leads' | 'tasks' | 'import' | 'performance' | 'history';

interface Stage { id: string; code: string; name_ar: string; name_en: string; sort_order: number; is_won: boolean; is_lost: boolean; is_converted: boolean; is_active: boolean; count?: number }
interface Lead { id: string; lead_no: string; full_name: string; company_name: string | null; phone: string | null; email: string | null; source: string; stage_code: string; priority: string; insurance_type: string; insurer_company_id: string | null; expected_premium: string | null; assigned_to: string | null; notes: string | null; lost_reason: string | null; converted_corporate_client_id: string | null; converted_at: string | null; closed_at: string | null; next_followup_at: string | null; created_at: string; activities?: Activity[]; tasks?: CrmTask[] }
interface Activity { id: string; type: string; description: string; task_id: string | null; created_at: string }
interface CrmTask { id: string; lead_id: string; lead_name?: string; lead_no?: string; type: string; title: string; description: string | null; due_at: string; status: string; is_overdue: boolean; assigned_to: string | null; completed_at: string | null }
interface LeadsPage { items: Lead[]; total: number; page: number; page_size: number; pages: number }
interface Board { stages: (Stage & { leads: Lead[]; value: string })[]; total: number; won: number; lost: number; converted: number; win_rate: number; conversion_rate: number; open_pipeline_value: string }
interface Batch { id: string; file_name: string; total_rows: number; success_rows: number; failed_rows: number; status: string; created_at: string }
interface PerfRow { staff_id: string; full_name: string; leads: number; won: number; lost: number; converted: number; conversion_rate: number; win_rate: number; won_value: string; pipeline_value: string; avg_close_days: string | null; tasks_done: number; tasks_overdue: number }
interface Stats { total_leads: number; by_stage: (Stage & { count: number })[]; by_source: Record<string, number>; last_30_days: { date: string; count: number }[]; conversion_rate: number; due_tasks: { overdue: number; today: number; next_7_days: number }; top_performers: PerfRow[]; recent_leads: Lead[]; recent_imports: Batch[]; recent_activities: Activity[] }
interface Preview { columns: string[]; total_rows: number; valid_rows: number; invalid_rows: number; preview: Record<string, string>[]; errors: { row_number: number; reason: string }[] }
interface ImportResult { batch_id: string; total_rows: number; success_rows: number; failed_rows: number; duplicates: number; status: string }
type Agent = { id: string; full_name: string };

const SOURCE_LABELS: Record<string, string> = { manual: 'يدوي', whatsapp: 'واتساب', call: 'مكالمة', email: 'إيميل', portal: 'البوابة', import: 'استيراد', other: 'أخرى' };
const PRIORITY_LABELS: Record<string, string> = { low: 'منخفضة', normal: 'عادية', high: 'عالية', urgent: 'عاجلة' };
const TASK_TYPE_LABELS: Record<string, string> = { call: 'مكالمة', meeting: 'ميتنج', email: 'إيميل', whatsapp: 'واتساب', task: 'مهمة عامة' };
const ACTIVITY_LABELS: Record<string, string> = { call: 'مكالمة', note: 'ملاحظة', stage_change: 'مرحلة', task: 'مهمة', conversion: 'تحويل', whatsapp: 'واتساب', email: 'إيميل', meeting: 'ميتنج', assignment: 'إسناد' };

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = name; anchor.click();
  URL.revokeObjectURL(url);
}

function uploadWithProgress(file: File, onProgress: (pct: number) => void): Promise<ImportResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/crm/imports');
    xhr.withCredentials = true;
    const token = getCsrfToken();
    if (token) xhr.setRequestHeader('X-CSRF-Token', token);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100)); };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText) as ImportResult & { detail?: string };
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error(body.detail ?? `فشل الاستيراد (${xhr.status})`));
      } catch { reject(new Error(`فشل الاستيراد (${xhr.status})`)); }
    };
    xhr.onerror = () => reject(new Error('تعذر الاتصال بالخادم.'));
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
}

function useStages(onError: (v: string) => void) {
  const [stages, setStages] = useState<Stage[]>([]);
  const reload = useCallback(async () => {
    try { setStages((await api<{ stages: Stage[] }>('/api/crm/stages?include_inactive=true')).stages); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل مراحل البايبلاين.'); }
  }, [onError]);
  useEffect(() => { const run = async () => { await reload(); }; void run(); }, [reload]);
  const label = useCallback((code: string) => stages.find((s) => s.code === code)?.name_ar ?? code, [stages]);
  return { stages, label, reloadStages: reload };
}

function useAgents(access: StaffAccess) {
  const [agents, setAgents] = useState<Agent[]>([]);
  useEffect(() => {
    const run = async () => {
      try {
        const users = await api<{ items?: Agent[] }>('/api/staff/users');
        setAgents(users.items ?? []);
      } catch { setAgents([{ id: access.id, full_name: access.full_name }]); }
    };
    void run();
  }, [access.id, access.full_name]);
  return agents;
}

export function CrmModulePage({ access, tab, onTab }: { access: StaffAccess; tab: CrmModuleTab; onTab: (tab: CrmModuleTab) => void }) {
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const canView = hasPermission(access, 'crm.view');
  const canManage = hasPermission(access, 'crm.manage');
  const canConvert = hasPermission(access, 'crm.convert');
  const agents = useAgents(access);
  const { stages, label: stageLabel, reloadStages } = useStages(setError);

  if (!canView) return <AccessDenied title="CRM غير متاح" />;
  const shared = { stages, stageLabel, agents, canManage, canConvert, onError: setError, onNotice: setNotice };
  return <div className="page-stack">
    {error && <div className="inline-alert error" role="alert">{error}</div>}
    {notice && <div className="inline-alert" role="status">{notice}</div>}
    {tab === 'dashboard' && <Dashboard onError={setError} stageLabel={stageLabel} />}
    {tab === 'pipeline' && <Pipeline {...shared} access={access} reloadStages={reloadStages} />}
    {tab === 'leads' && <Leads {...shared} />}
    {tab === 'tasks' && <Tasks {...shared} />}
    {tab === 'import' && <ImportLeads canManage={canManage} onError={setError} onNotice={setNotice} onDone={() => onTab('history')} />}
    {tab === 'performance' && <Performance onError={setError} />}
    {tab === 'history' && <ImportHistory onError={setError} />}
  </div>;
}

type SharedProps = {
  stages: Stage[]; stageLabel: (code: string) => string; agents: Agent[];
  canManage: boolean; canConvert: boolean;
  onError: (v: string) => void; onNotice: (v: string) => void;
};

function Dashboard({ onError, stageLabel }: { onError: (v: string) => void; stageLabel: (code: string) => string }) {
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    const load = async () => {
      try { setStats(await api<Stats>('/api/crm/stats')); }
      catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل إحصاءات CRM.'); }
    };
    void load();
  }, [onError]);
  if (!stats) return <div className="loading-surface"><span className="loading-spinner" /> جارٍ تحميل لوحة CRM…</div>;
  const stageData = stats.by_stage.map((stage) => ({ name: stage.name_ar, count: stage.count }));
  const sourceData = Object.entries(stats.by_source).map(([key, count]) => ({ name: SOURCE_LABELS[key] ?? key, count }));
  return <>
    <section className="surface-card user-summary-strip">
      <div className="summary-stat"><div><strong>{formatNumber(stats.total_leads)}</strong><small>إجمالي العملاء المحتملين</small></div></div>
      <div className="summary-stat"><div><strong>{stats.conversion_rate}٪</strong><small>معدل التحويل لعميل</small></div></div>
      <div className="summary-stat"><div><strong>{formatNumber(stats.last_30_days.reduce((sum, day) => sum + day.count, 0))}</strong><small>جدد آخر ٣٠ يوماً</small></div></div>
      <div className="summary-stat"><div><strong className={stats.due_tasks.overdue ? 'crmm-overdue' : ''}>{formatNumber(stats.due_tasks.overdue)}</strong><small>مهام متأخرة · اليوم {formatNumber(stats.due_tasks.today)}</small></div></div>
    </section>
    <div className="crmm-grid">
      <section className="surface-card"><h3>حسب مرحلة البايبلاين</h3><ResponsiveContainer width="100%" height={220}><BarChart data={stageData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="name" interval={0} angle={-20} textAnchor="end" height={60} /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#5e768e" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer></section>
      <section className="surface-card"><h3>حسب المصدر</h3><ResponsiveContainer width="100%" height={220}><BarChart data={sourceData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="name" /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#b88a2b" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer></section>
      <section className="surface-card"><h3>الجدد آخر ٣٠ يوماً</h3><ResponsiveContainer width="100%" height={220}><BarChart data={stats.last_30_days}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="date" hide /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#2e8a7d" /></BarChart></ResponsiveContainer></section>
    </div>
    <div className="crmm-grid">
      <section className="surface-card"><h3>أحدث العملاء المحتملين</h3>{stats.recent_leads.map((lead) => <p key={lead.id}>{lead.company_name ?? lead.full_name} · {stageLabel(lead.stage_code)} · <span dir="ltr">{lead.phone ?? ''}</span></p>)}{!stats.recent_leads.length && <div className="empty-inline">لا بيانات بعد — استورد ملف Excel للبدء.</div>}</section>
      <section className="surface-card"><h3>أفضل البياعين</h3>{stats.top_performers.map((row) => <p key={row.staff_id}>{row.full_name} · تحويل {row.conversion_rate}٪ · صفقات {row.won_value}</p>)}{!stats.top_performers.length && <div className="empty-inline">لا أداء مسجل بعد.</div>}</section>
      <section className="surface-card"><h3>أحدث الأنشطة</h3>{stats.recent_activities.map((activity) => <p key={activity.id}>{ACTIVITY_LABELS[activity.type] ?? activity.type}: {activity.description} <small>({formatDateTime(activity.created_at)})</small></p>)}</section>
      <section className="surface-card"><h3>أحدث الاستيرادات</h3>{stats.recent_imports.map((batch) => <p key={batch.id}>{batch.file_name} · ناجح {batch.success_rows}/{batch.total_rows} · {batch.status}</p>)}</section>
    </div>
  </>;
}

function Pipeline({ access, stages, stageLabel, agents, canManage, canConvert, onError, onNotice, reloadStages }: SharedProps & { access: StaffAccess; reloadStages: () => Promise<void> }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [assignedTo, setAssignedTo] = useState('');
  const [source, setSource] = useState('');
  const [detail, setDetail] = useState<Lead | null>(null);
  const [dragOver, setDragOver] = useState('');
  const dragged = useRef<string>('');

  const load = useCallback(async () => {
    const query = new URLSearchParams();
    if (assignedTo) query.set('assigned_to', assignedTo);
    if (source) query.set('source', source);
    try { setBoard(await api<Board>(`/api/crm/pipeline${query.size ? `?${query}` : ''}`)); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل البايبلاين.'); }
  }, [assignedTo, source, onError]);
  useEffect(() => { const run = async () => { await load(); }; void run(); }, [load]);

  const move = async (leadId: string, stageCode: string) => {
    try {
      await api(`/api/crm/leads/${leadId}/move`, { method: 'POST', body: jsonBody({ stage_code: stageCode }) });
      onNotice(`نُقل العميل المحتمل إلى «${stageLabel(stageCode)}».`);
      await load();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر نقل المرحلة.'); }
  };
  const openLead = async (id: string) => { try { setDetail(await api<Lead>(`/api/crm/leads/${id}`)); } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الفتح.'); } };

  return <>
    <section className="surface-card user-summary-strip">
      <div className="summary-stat"><div><strong>{formatNumber(board?.total ?? 0)}</strong><small>إجمالي العملاء المحتملين</small></div></div>
      <div className="summary-stat"><div><strong>{board?.win_rate ?? 0}٪</strong><small>معدل الربح</small></div></div>
      <div className="summary-stat"><div><strong>{board?.conversion_rate ?? 0}٪</strong><small>معدل التحويل لعميل</small></div></div>
      <div className="summary-stat"><div><strong>{board?.open_pipeline_value ?? '0'}</strong><small>قيمة البايبلاين المفتوحة (ج.م)</small></div></div>
    </section>
    <section className="surface-card"><div className="crmm-toolbar">
      <label className="field-label">المندوب<select value={assignedTo} onChange={(e) => setAssignedTo(e.target.value)}><option value="">الكل</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
      <label className="field-label">المصدر<select value={source} onChange={(e) => setSource(e.target.value)}><option value="">الكل</option>{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <span className="muted">اسحب البطاقة وأفلتها في عمود آخر لنقل المرحلة.</span>
    </div></section>
    <div className="crmm-kanban" aria-label="لوحة خط المبيعات">
      {(board?.stages ?? []).map((stage) => (
        <div
          key={stage.code}
          className={`crmm-column${dragOver === stage.code ? ' is-over' : ''}`}
          data-stage={stage.code}
          onDragOver={(e) => { e.preventDefault(); setDragOver(stage.code); }}
          onDragLeave={() => setDragOver('')}
          onDrop={(e) => { e.preventDefault(); setDragOver(''); const id = e.dataTransfer.getData('text/lead-id') || dragged.current; if (id) void move(id, stage.code); }}
        >
          <h4>{stage.name_ar} <span>{stage.count ?? 0}</span></h4>
          {stage.leads.map((lead) => (
            <article
              key={lead.id}
              className="crmm-card"
              draggable={canManage}
              onDragStart={(e) => { dragged.current = lead.id; e.dataTransfer.setData('text/lead-id', lead.id); e.dataTransfer.effectAllowed = 'move'; }}
              onClick={() => void openLead(lead.id)}
            >
              <strong>{lead.company_name ?? lead.full_name}</strong>
              <small>{lead.lead_no} · {SOURCE_LABELS[lead.source] ?? lead.source} · {PRIORITY_LABELS[lead.priority] ?? lead.priority}</small>
              <small>{lead.full_name} · <span dir="ltr">{lead.phone ?? ''}</span></small>
              {lead.expected_premium && <small>قسط متوقع: {lead.expected_premium}</small>}
              {lead.next_followup_at && <small className={new Date(lead.next_followup_at) <= new Date() ? 'crmm-overdue' : ''}>متابعة: {formatDateTime(lead.next_followup_at)}</small>}
              {canManage && <select aria-label="نقل المرحلة" value={lead.stage_code} onClick={(e) => e.stopPropagation()} onChange={(e) => void move(lead.id, e.target.value)}>{stages.filter((s) => s.is_active).map((s) => <option key={s.code} value={s.code}>{s.name_ar}</option>)}</select>}
            </article>
          ))}
          {!stage.leads.length && <small className="muted">لا عملاء هنا.</small>}
        </div>
      ))}
    </div>
    {canManage && <StageManager stages={stages} onChanged={async () => { await reloadStages(); await load(); }} onError={onError} onNotice={onNotice} />}
    {detail && <LeadDrawer lead={detail} stages={stages} agents={agents} canManage={canManage} canConvert={canConvert} access={access} onClose={() => setDetail(null)} onChanged={async () => { await openLead(detail.id); await load(); }} onError={onError} onNotice={onNotice} />}
  </>;
}

function StageManager({ stages, onChanged, onError, onNotice }: { stages: Stage[]; onChanged: () => Promise<void>; onError: (v: string) => void; onNotice: (v: string) => void }) {
  const run = async (call: () => Promise<unknown>, message: string) => {
    try { await call(); onNotice(message); await onChanged(); } catch (err) { onError(err instanceof Error ? err.message : 'تعذر حفظ المرحلة.'); }
  };
  const add = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const kind = String(form.get('kind'));
    void run(() => api('/api/crm/stages', {
      method: 'POST',
      body: jsonBody({
        code: form.get('code'), name_ar: form.get('name_ar'), name_en: form.get('name_en'),
        sort_order: (stages.length + 1) * 10,
        is_won: kind === 'won', is_lost: kind === 'lost', is_converted: kind === 'converted',
      }),
    }), 'تمت إضافة المرحلة.');
    event.currentTarget.reset();
  };
  const moveStage = (index: number, delta: number) => {
    const ids = stages.map((s) => s.id);
    const target = index + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    void run(() => api('/api/crm/stages/reorder', { method: 'POST', body: jsonBody({ ids }) }), 'تمت إعادة ترتيب المراحل.');
  };
  return <section className="surface-card"><h3>تخصيص المراحل</h3>
    {stages.map((stage, index) => <div key={stage.id} className="crmm-stage-row">
      <span><strong>{stage.name_ar}</strong> <small>({stage.code}{stage.is_won ? ' · ربح' : stage.is_lost ? ' · خسارة' : stage.is_converted ? ' · محوّل' : ''}{stage.is_active ? '' : ' · معطلة'}) · {stage.count ?? 0} عميل</small></span>
      <span className="crmm-stage-actions">
        <button className="button button-light button-small" type="button" aria-label="لأعلى" onClick={() => moveStage(index, -1)}>↑</button>
        <button className="button button-light button-small" type="button" aria-label="لأسفل" onClick={() => moveStage(index, 1)}>↓</button>
        <button className="button button-light button-small" type="button" onClick={() => void run(() => api(`/api/crm/stages/${stage.id}`, { method: 'PATCH', body: jsonBody({ is_active: !stage.is_active }) }), stage.is_active ? 'تم تعطيل المرحلة.' : 'تم تفعيل المرحلة.')}>{stage.is_active ? 'تعطيل' : 'تفعيل'}</button>
        <button className="button button-light button-small" type="button" onClick={() => void run(() => api(`/api/crm/stages/${stage.id}`, { method: 'DELETE' }), 'حُذفت المرحلة الفارغة.')}>حذف</button>
      </span>
    </div>)}
    <form className="crmm-toolbar" onSubmit={add}>
      <label className="field-label">الكود<input name="code" required pattern="[a-z][a-z0-9_]*" dir="ltr" placeholder="site_visit" /></label>
      <label className="field-label">الاسم بالعربي<input name="name_ar" required /></label>
      <label className="field-label">Name (EN)<input name="name_en" required dir="ltr" /></label>
      <label className="field-label">النوع<select name="kind"><option value="open">مفتوحة</option><option value="won">ربح</option><option value="lost">خسارة</option><option value="converted">محوّل لعميل</option></select></label>
      <button className="button button-dark" type="submit">إضافة مرحلة</button>
    </form>
    <small className="muted">الحذف محمي: لا يمكن حذف مرحلة بها عملاء أو مرحلة الاستقبال «جديد» — عطّلها بدلاً من ذلك.</small>
  </section>;
}

function Leads({ stages, stageLabel, agents, canManage, canConvert, onError, onNotice }: SharedProps) {
  const [data, setData] = useState<LeadsPage | null>(null);
  const [search, setSearch] = useState('');
  const [stage, setStage] = useState('');
  const [source, setSource] = useState('');
  const [assignedTo, setAssignedTo] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState('created_at');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<Lead | null>(null);
  const [bulkAssignee, setBulkAssignee] = useState('');
  const [bulkStage, setBulkStage] = useState('contacted');

  const load = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page), page_size: '20', sort, order });
    if (search) query.set('search', search);
    if (stage) query.set('stage_code', stage);
    if (source) query.set('source', source);
    if (assignedTo) query.set('assigned_to', assignedTo);
    try { setData(await api<LeadsPage>(`/api/crm/leads?${query}`)); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل العملاء المحتملين.'); }
  }, [search, stage, source, assignedTo, page, sort, order, onError]);
  useEffect(() => { const run = async () => { await load(); }; void run(); }, [load]);

  const toggleSort = (field: string) => { if (sort === field) setOrder((value) => (value === 'asc' ? 'desc' : 'asc')); else { setSort(field); setOrder('asc'); } };
  const toggle = (id: string) => setSelected((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const bulk = async (path: string, body: Record<string, unknown>, message: string) => {
    try { await api(path, { method: 'POST', body: jsonBody(body) }); onNotice(message); setSelected(new Set()); await load(); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تنفيذ العملية.'); }
  };
  const openLead = async (id: string) => { try { setDetail(await api<Lead>(`/api/crm/leads/${id}`)); } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الفتح.'); } };
  const remove = async (lead: Lead) => {
    if (!window.confirm(`حذف «${lead.company_name ?? lead.full_name}» نهائياً؟`)) return;
    try { await api(`/api/crm/leads/${lead.id}`, { method: 'DELETE' }); onNotice('تم حذف العميل المحتمل.'); await load(); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر الحذف.'); }
  };
  const createLead = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api('/api/crm/leads', {
        method: 'POST',
        body: jsonBody({
          full_name: form.get('full_name'), company_name: form.get('company_name') || null,
          phone: form.get('phone') || null, email: form.get('email') || null,
          source: form.get('source'), priority: form.get('priority'),
          insurance_type: form.get('insurance_type'),
          expected_premium: form.get('expected_premium') || null,
          notes: form.get('notes') || null,
        }),
      });
      onNotice('تمت إضافة العميل المحتمل.'); event.currentTarget.reset(); await load();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الإضافة.'); }
  };

  return <>
    {canManage && <section className="surface-card"><h3>إضافة Lead</h3><form className="crmm-toolbar" onSubmit={createLead}>
      <label className="field-label">الاسم / جهة الاتصال<input name="full_name" required minLength={2} /></label>
      <label className="field-label">الشركة (اختياري)<input name="company_name" /></label>
      <label className="field-label">الهاتف<input name="phone" dir="ltr" placeholder="010…" /></label>
      <label className="field-label">البريد<input name="email" type="email" dir="ltr" /></label>
      <label className="field-label">المصدر<select name="source">{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">الأولوية<select name="priority">{Object.entries(PRIORITY_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">نوع التأمين<select name="insurance_type"><option value="health">طبي</option><option value="general">عام</option><option value="both">كلاهما</option></select></label>
      <label className="field-label">القسط المتوقع<input name="expected_premium" type="number" min="0" step="0.01" /></label>
      <label className="field-label">ملاحظات<input name="notes" /></label>
      <button className="button button-dark" type="submit">إضافة</button>
    </form></section>}
    <section className="surface-card"><div className="crmm-toolbar">
      <label className="field-label">بحث<input value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} placeholder="اسم / شركة / هاتف / بريد / رقم" /></label>
      <label className="field-label">المرحلة<select value={stage} onChange={(e) => { setStage(e.target.value); setPage(1); }}><option value="">الكل</option>{stages.map((s) => <option key={s.code} value={s.code}>{s.name_ar}</option>)}</select></label>
      <label className="field-label">المصدر<select value={source} onChange={(e) => { setSource(e.target.value); setPage(1); }}><option value="">الكل</option>{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">المندوب<select value={assignedTo} onChange={(e) => { setAssignedTo(e.target.value); setPage(1); }}><option value="">الكل</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
      {canManage && selected.size > 0 && <>
        <label className="field-label">إسناد جماعي<select value={bulkAssignee} onChange={(e) => setBulkAssignee(e.target.value)}><option value="">— إلغاء الإسناد —</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
        <button className="button button-light" type="button" onClick={() => void bulk('/api/crm/leads/bulk-assign', { ids: [...selected], assigned_to: bulkAssignee || null }, 'تم الإسناد الجماعي.')}>إسناد ({selected.size})</button>
        <label className="field-label">نقل جماعي لمرحلة<select value={bulkStage} onChange={(e) => setBulkStage(e.target.value)}>{stages.filter((s) => s.is_active).map((s) => <option key={s.code} value={s.code}>{s.name_ar}</option>)}</select></label>
        <button className="button button-light" type="button" onClick={() => void bulk('/api/crm/leads/bulk-stage', { ids: [...selected], stage_code: bulkStage }, 'تم نقل المرحلة جماعياً.')}>نقل ({selected.size})</button>
      </>}
    </div></section>
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr>
      <th></th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('full_name')}>الاسم / الشركة</button></th>
      <th>الهاتف</th><th>المصدر</th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('stage_code')}>المرحلة</button></th>
      <th>الأولوية</th><th>القسط المتوقع</th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('next_followup_at')}>المتابعة</button></th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('created_at')}>التاريخ</button></th><th></th>
    </tr></thead><tbody>
      {(data?.items ?? []).map((lead) => <tr key={lead.id}>
        <td><input type="checkbox" aria-label={`تحديد ${lead.full_name}`} checked={selected.has(lead.id)} onChange={() => toggle(lead.id)} /></td>
        <td><button type="button" className="link-button" onClick={() => void openLead(lead.id)}><strong>{lead.company_name ?? lead.full_name}</strong></button><br /><small>{lead.lead_no}{lead.company_name ? ` · ${lead.full_name}` : ''}</small></td>
        <td dir="ltr">{lead.phone ?? '—'}</td>
        <td>{SOURCE_LABELS[lead.source] ?? lead.source}</td>
        <td>{stageLabel(lead.stage_code)}</td>
        <td>{PRIORITY_LABELS[lead.priority] ?? lead.priority}</td>
        <td>{lead.expected_premium ?? '—'}</td>
        <td className={lead.next_followup_at && new Date(lead.next_followup_at) <= new Date() ? 'crmm-overdue' : ''}>{lead.next_followup_at ? formatDateTime(lead.next_followup_at) : '—'}</td>
        <td>{formatDateTime(lead.created_at)}</td>
        <td>{canManage && <button type="button" className="button button-light button-small" onClick={() => void remove(lead)}>حذف</button>}</td>
      </tr>)}
      {data && !data.items.length && <tr><td colSpan={10}><div className="empty-inline">لا يوجد عملاء محتملون مطابقون.</div></td></tr>}
    </tbody></table></div>
      <div className="crmm-pager">
        <button className="button button-light button-small" type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>السابق</button>
        <span>صفحة {data?.page ?? page} من {data?.pages ?? 0} · {formatNumber(data?.total ?? 0)}</span>
        <button className="button button-light button-small" type="button" disabled={!data || page >= data.pages} onClick={() => setPage((p) => p + 1)}>التالي</button>
      </div>
    </section>
    {detail && <LeadDrawer lead={detail} stages={stages} agents={agents} canManage={canManage} canConvert={canConvert} onClose={() => setDetail(null)} onChanged={async () => { await openLead(detail.id); await load(); }} onError={onError} onNotice={onNotice} />}
  </>;
}

function LeadDrawer({ lead, stages, agents, canManage, canConvert, onClose, onChanged, onError, onNotice }: {
  lead: Lead; stages: Stage[]; agents: Agent[]; canManage: boolean; canConvert: boolean;
  access?: StaffAccess; onClose: () => void; onChanged: () => Promise<void>;
  onError: (v: string) => void; onNotice: (v: string) => void;
}) {
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api(`/api/crm/leads/${lead.id}`, {
        method: 'PATCH',
        body: jsonBody({
          full_name: form.get('full_name'), company_name: form.get('company_name') || null,
          phone: form.get('phone') || null, email: form.get('email') || null,
          stage_code: form.get('stage_code'), priority: form.get('priority'),
          source: form.get('source'), insurance_type: form.get('insurance_type'),
          expected_premium: form.get('expected_premium') || null,
          assigned_to: form.get('assigned_to') || null, notes: form.get('notes') || null,
          lost_reason: form.get('lost_reason') || null,
        }),
      });
      await onChanged();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الحفظ.'); }
  };
  const addActivity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api(`/api/crm/leads/${lead.id}/activities`, { method: 'POST', body: jsonBody({ type: form.get('type'), description: form.get('description') }) });
      event.currentTarget.reset(); await onChanged();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر تسجيل النشاط.'); }
  };
  const addTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const due = String(form.get('due_at') ?? '');
    try {
      await api(`/api/crm/leads/${lead.id}/tasks`, {
        method: 'POST',
        body: jsonBody({ type: form.get('type'), title: form.get('title'), due_at: due ? new Date(due).toISOString() : null }),
      });
      event.currentTarget.reset(); await onChanged();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر جدولة المهمة.'); }
  };
  const completeTask = async (task: CrmTask) => {
    try { await api(`/api/crm/tasks/${task.id}/complete`, { method: 'POST', body: jsonBody({}) }); await onChanged(); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر إنجاز المهمة.'); }
  };
  const convert = async () => {
    try {
      await api(`/api/crm/leads/${lead.id}/convert`, { method: 'POST', body: jsonBody({}) });
      onNotice('تم تحويل العميل المحتمل إلى عميل فعلي بكل بياناته (ملف عميل جاهز).');
      await onChanged();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر التحويل.'); }
  };
  return <Modal title={lead.company_name ?? lead.full_name} eyebrow={lead.lead_no} onClose={onClose} wide>
    {lead.converted_corporate_client_id
      ? <p className="inline-alert" role="status">محوّل إلى عميل فعلي — افتح «ملف العميل» من مساحة الوثائق والإصدار لعرض العقد والوثائق والموظفين.</p>
      : canConvert && <button className="button button-dark" type="button" onClick={() => void convert()}>تحويل إلى عميل فعلي</button>}
    {canManage ? <form className="crmm-toolbar" onSubmit={save}>
      <label className="field-label">الاسم<input name="full_name" defaultValue={lead.full_name} required /></label>
      <label className="field-label">الشركة<input name="company_name" defaultValue={lead.company_name ?? ''} /></label>
      <label className="field-label">الهاتف<input name="phone" dir="ltr" defaultValue={lead.phone ?? ''} /></label>
      <label className="field-label">البريد<input name="email" dir="ltr" defaultValue={lead.email ?? ''} /></label>
      <label className="field-label">المرحلة<select name="stage_code" defaultValue={lead.stage_code}>{stages.filter((s) => s.is_active).map((s) => <option key={s.code} value={s.code}>{s.name_ar}</option>)}</select></label>
      <label className="field-label">الأولوية<select name="priority" defaultValue={lead.priority}>{Object.entries(PRIORITY_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">المصدر<select name="source" defaultValue={lead.source}>{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">نوع التأمين<select name="insurance_type" defaultValue={lead.insurance_type}><option value="health">طبي</option><option value="general">عام</option><option value="both">كلاهما</option></select></label>
      <label className="field-label">القسط المتوقع<input name="expected_premium" type="number" min="0" step="0.01" defaultValue={lead.expected_premium ?? ''} /></label>
      <label className="field-label">المندوب<select name="assigned_to" defaultValue={lead.assigned_to ?? ''}><option value="">غير مسند</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
      <label className="field-label">سبب الخسارة<input name="lost_reason" defaultValue={lead.lost_reason ?? ''} /></label>
      <label className="field-label">ملاحظات<input name="notes" defaultValue={lead.notes ?? ''} /></label>
      <button className="button button-dark" type="submit">حفظ</button>
    </form> : <p>{lead.phone ?? ''} · {lead.email ?? ''}</p>}
    <h4>مهام ومتابعات هذا العميل</h4>
    <ul className="crmm-timeline">{(lead.tasks ?? []).map((task) => <li key={task.id}>
      <strong>{TASK_TYPE_LABELS[task.type] ?? task.type}</strong> — {task.title}
      <span className={task.is_overdue ? ' crmm-badge crmm-badge-overdue' : ' crmm-badge'}>{task.status === 'done' ? 'منجزة' : task.is_overdue ? 'متأخرة' : 'معلّقة'}</span>
      <br /><small>تستحق {formatDateTime(task.due_at)}</small>
      {canManage && task.status === 'pending' && <> <button className="button button-light button-small" type="button" onClick={() => void completeTask(task)}>إنجاز</button></>}
    </li>)}</ul>
    {canManage && <form className="crmm-toolbar" onSubmit={addTask}>
      <label className="field-label">نوع المهمة<select name="type">{Object.entries(TASK_TYPE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">العنوان<input name="title" required /></label>
      <label className="field-label">الموعد<input name="due_at" type="datetime-local" required /></label>
      <button className="button button-light" type="submit">جدولة مهمة</button>
    </form>}
    <h4>سجل الأنشطة</h4>
    <ul className="crmm-timeline">{(lead.activities ?? []).map((activity) => <li key={activity.id}><strong>{ACTIVITY_LABELS[activity.type] ?? activity.type}</strong> — {activity.description}<br /><small>{formatDateTime(activity.created_at)}</small></li>)}</ul>
    {canManage && <form className="crmm-toolbar" onSubmit={addActivity}>
      <label className="field-label">النوع<select name="type"><option value="call">مكالمة</option><option value="note">ملاحظة</option><option value="whatsapp">واتساب</option><option value="email">إيميل</option><option value="meeting">ميتنج</option></select></label>
      <label className="field-label">الوصف<input name="description" required /></label>
      <button className="button button-light" type="submit">تسجيل نشاط</button>
    </form>}
  </Modal>;
}

function Tasks({ agents, canManage, onError, onNotice }: SharedProps) {
  const [tasks, setTasks] = useState<CrmTask[]>([]);
  const [dueCounts, setDueCounts] = useState<{ overdue: number; today: number; next_7_days: number }>({ overdue: 0, today: 0, next_7_days: 0 });
  const [due, setDue] = useState('');
  const [status, setStatus] = useState('');
  const [assignedTo, setAssignedTo] = useState('');
  const [leadOptions, setLeadOptions] = useState<Lead[]>([]);

  const load = useCallback(async () => {
    const query = new URLSearchParams();
    if (due) query.set('due', due);
    if (status) query.set('status', status);
    if (assignedTo) query.set('assigned_to', assignedTo);
    try {
      const result = await api<{ tasks: CrmTask[]; due_counts: typeof dueCounts }>(`/api/crm/tasks${query.size ? `?${query}` : ''}`);
      setTasks(result.tasks); setDueCounts(result.due_counts);
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل المهام.'); }
  }, [due, status, assignedTo, onError]);
  useEffect(() => { const run = async () => { await load(); }; void run(); }, [load]);
  useEffect(() => {
    const run = async () => { try { setLeadOptions((await api<LeadsPage>('/api/crm/leads?page_size=100')).items); } catch { setLeadOptions([]); } };
    void run();
  }, []);

  const act = async (call: () => Promise<unknown>, message: string) => {
    try { await call(); onNotice(message); await load(); } catch (err) { onError(err instanceof Error ? err.message : 'تعذر تنفيذ العملية.'); }
  };
  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const dueValue = String(form.get('due_at') ?? '');
    await act(() => api(`/api/crm/leads/${String(form.get('lead_id'))}/tasks`, {
      method: 'POST',
      body: jsonBody({ type: form.get('type'), title: form.get('title'), due_at: dueValue ? new Date(dueValue).toISOString() : null }),
    }), 'تمت جدولة المهمة.');
    event.currentTarget.reset();
  };
  const agentName = (id: string | null) => agents.find((a) => a.id === id)?.full_name ?? '—';

  return <>
    <section className="surface-card user-summary-strip">
      <div className="summary-stat"><div><strong className={dueCounts.overdue ? 'crmm-overdue' : ''}>{formatNumber(dueCounts.overdue)}</strong><small>متأخرة</small></div></div>
      <div className="summary-stat"><div><strong>{formatNumber(dueCounts.today)}</strong><small>تستحق اليوم</small></div></div>
      <div className="summary-stat"><div><strong>{formatNumber(dueCounts.next_7_days)}</strong><small>خلال ٧ أيام</small></div></div>
    </section>
    {canManage && <section className="surface-card"><h3>مهمة جديدة</h3><form className="crmm-toolbar" onSubmit={create}>
      <label className="field-label">العميل المحتمل<select name="lead_id" required>{leadOptions.map((lead) => <option key={lead.id} value={lead.id}>{lead.company_name ?? lead.full_name} · {lead.lead_no}</option>)}</select></label>
      <label className="field-label">النوع<select name="type">{Object.entries(TASK_TYPE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">العنوان<input name="title" required /></label>
      <label className="field-label">الموعد<input name="due_at" type="datetime-local" required /></label>
      <button className="button button-dark" type="submit">جدولة</button>
    </form></section>}
    <section className="surface-card"><div className="crmm-toolbar">
      <label className="field-label">الاستحقاق<select value={due} onChange={(e) => setDue(e.target.value)}><option value="">الكل</option><option value="overdue">متأخرة</option><option value="today">اليوم</option><option value="upcoming">قادمة</option></select></label>
      <label className="field-label">الحالة<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">الكل</option><option value="pending">معلّقة</option><option value="done">منجزة</option><option value="cancelled">ملغاة</option></select></label>
      <label className="field-label">المندوب<select value={assignedTo} onChange={(e) => setAssignedTo(e.target.value)}><option value="">الكل</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
    </div></section>
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr>
      <th>المهمة</th><th>العميل المحتمل</th><th>النوع</th><th>الموعد</th><th>الحالة</th><th>المندوب</th><th></th>
    </tr></thead><tbody>
      {tasks.map((task) => <tr key={task.id}>
        <td><strong>{task.title}</strong>{task.description ? <><br /><small>{task.description}</small></> : null}</td>
        <td>{task.lead_name ?? task.lead_id}<br /><small>{task.lead_no ?? ''}</small></td>
        <td>{TASK_TYPE_LABELS[task.type] ?? task.type}</td>
        <td>{formatDateTime(task.due_at)}</td>
        <td><span className={`crmm-badge${task.is_overdue ? ' crmm-badge-overdue' : ''}`}>{task.status === 'done' ? 'منجزة' : task.status === 'cancelled' ? 'ملغاة' : task.is_overdue ? 'متأخرة' : 'معلّقة'}</span></td>
        <td>{agentName(task.assigned_to)}</td>
        <td>{canManage && task.status === 'pending' && <>
          <button className="button button-light button-small" type="button" onClick={() => void act(() => api(`/api/crm/tasks/${task.id}/complete`, { method: 'POST', body: jsonBody({}) }), 'أُنجزت المهمة.')}>إنجاز</button>{' '}
          <button className="button button-light button-small" type="button" onClick={() => void act(() => api(`/api/crm/tasks/${task.id}`, { method: 'DELETE' }), 'حُذفت المهمة.')}>حذف</button>
        </>}</td>
      </tr>)}
      {!tasks.length && <tr><td colSpan={7}><div className="empty-inline">لا توجد مهام مطابقة.</div></td></tr>}
    </tbody></table></div></section>
  </>;
}

function Performance({ onError }: { onError: (v: string) => void }) {
  const [rows, setRows] = useState<PerfRow[]>([]);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const load = useCallback(async () => {
    const query = new URLSearchParams();
    if (from) query.set('date_from', new Date(from).toISOString());
    if (to) query.set('date_to', new Date(to).toISOString());
    try { setRows((await api<{ salespeople: PerfRow[] }>(`/api/crm/reports/sales-performance${query.size ? `?${query}` : ''}`)).salespeople); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل أداء البياعين.'); }
  }, [from, to, onError]);
  useEffect(() => { const run = async () => { await load(); }; void run(); }, [load]);
  return <>
    <section className="surface-card"><div className="crmm-toolbar">
      <label className="field-label">من تاريخ<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
      <label className="field-label">إلى تاريخ<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
    </div></section>
    <section className="surface-card"><h3>معدل التحويل وقيمة الصفقات لكل بياع</h3>
      <ResponsiveContainer width="100%" height={240}><BarChart data={rows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="full_name" /><YAxis /><Tooltip /><Bar dataKey="conversion_rate" fill="#5e768e" name="معدل التحويل ٪" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer>
    </section>
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr>
      <th>البياع</th><th>Leads</th><th>ربح</th><th>خسارة</th><th>تحويل</th><th>معدل التحويل</th><th>قيمة الصفقات الرابحة</th><th>قيمة البايبلاين</th><th>متوسط زمن الإغلاق (يوم)</th><th>مهام منجزة</th><th>مهام متأخرة</th>
    </tr></thead><tbody>
      {rows.map((row) => <tr key={row.staff_id}>
        <td><strong>{row.full_name}</strong></td><td>{formatNumber(row.leads)}</td><td>{formatNumber(row.won)}</td><td>{formatNumber(row.lost)}</td>
        <td>{formatNumber(row.converted)}</td><td>{row.conversion_rate}٪</td><td>{row.won_value}</td><td>{row.pipeline_value}</td>
        <td>{row.avg_close_days ?? '—'}</td><td>{formatNumber(row.tasks_done)}</td>
        <td className={row.tasks_overdue ? 'crmm-overdue' : ''}>{formatNumber(row.tasks_overdue)}</td>
      </tr>)}
      {!rows.length && <tr><td colSpan={11}><div className="empty-inline">لا توجد بيانات أداء بعد.</div></td></tr>}
    </tbody></table></div></section>
  </>;
}

function ImportLeads({ canManage, onError, onNotice, onDone }: { canManage: boolean; onError: (v: string) => void; onNotice: (v: string) => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const pick = (candidate: File | null) => { setFile(candidate); setPreview(null); setResult(null); };
  const onDrop = (event: DragEvent) => { event.preventDefault(); setOver(false); pick(event.dataTransfer.files?.[0] ?? null); };
  const doPreview = async () => {
    if (!file) return;
    const form = new FormData(); form.append('file', file);
    try { setPreview(await api<Preview>('/api/crm/imports/preview', { method: 'POST', body: form })); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذرت المعاينة.'); }
  };
  const doImport = async () => {
    if (!file) return;
    setProgress(0);
    try {
      const summary = await uploadWithProgress(file, setProgress);
      setResult(summary); onNotice(`اكتمل الاستيراد: ناجح ${summary.success_rows} · فشل ${summary.failed_rows} (تكرار ${summary.duplicates}).`);
    } catch (err) { onError(err instanceof Error ? err.message : 'فشل الاستيراد.'); }
    finally { setProgress(null); }
  };
  const template = async () => {
    try { download(await (await apiResponse('/api/crm/imports/template.xlsx')).blob(), 'crm-leads-template.xlsx'); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تنزيل القالب.'); }
  };
  const errorsCsv = async () => {
    if (!result) return;
    try { download(await (await apiResponse(`/api/crm/imports/${result.batch_id}/errors.csv`)).blob(), 'crm-import-errors.csv'); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تنزيل تقرير الأخطاء.'); }
  };

  if (!canManage) return <AccessDenied title="الاستيراد غير متاح" />;
  return <>
    <section className="surface-card">
      <div className={`crmm-drop${over ? ' is-over' : ''}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop} role="button" tabIndex={0} onClick={() => inputRef.current?.click()} onKeyDown={() => inputRef.current?.click()} aria-label="رفع ملف Excel">
        <strong>اسحب ملف Excel هنا أو اضغط للاختيار</strong>
        <p className="muted">.xlsx / .xls / .csv — بحد أقصى ٥ ميجابايت</p>
        {file && <p>{file.name} · {formatNumber(file.size)} بايت</p>}
        <input ref={inputRef} type="file" accept=".xlsx,.xls,.csv" hidden onChange={(e) => pick(e.target.files?.[0] ?? null)} />
      </div>
      <div className="modal-actions">
        <button className="button button-light" type="button" onClick={() => void template()}>تنزيل قالب Excel</button>
        <button className="button button-light" type="button" disabled={!file} onClick={() => void doPreview()}>معاينة</button>
        <button className="button button-dark" type="button" disabled={!file || progress !== null} onClick={() => void doImport()}>استيراد</button>
      </div>
      {progress !== null && <div className="crmm-progress" role="progressbar" aria-valuenow={progress}><div style={{ width: `${progress}%` }} /></div>}
      <small className="muted">الأعمدة: full_name (الاسم)، phone (الهاتف)، email (البريد)، source (المصدر)، notes (ملاحظات). يدخل المستورَد مرحلة «جديد»، وتُطوى أرقام 01… إلى ‎+20‎ وتُعلَّم الصفوف المكررة/غير الصالحة كأخطاء بدل إدخالها.</small>
    </section>
    {preview && <section className="surface-card"><h3>معاينة ({preview.total_rows} صف — صالح {preview.valid_rows} · غير صالح {preview.invalid_rows})</h3>
      <div className="table-wrap"><table className="data-table"><thead><tr>{preview.columns.map((col) => <th key={col}>{col}</th>)}</tr></thead>
      <tbody>{preview.preview.map((row, index) => <tr key={index}>{preview.columns.map((col) => <td key={col}>{row[col] ?? ''}</td>)}</tr>)}</tbody></table></div>
    </section>}
    {result && <section className="surface-card"><h3>نتيجة الاستيراد</h3>
      <p>الإجمالي {result.total_rows} · ناجح {result.success_rows} · فشل {result.failed_rows} · تكرار {result.duplicates} · الحالة {result.status}</p>
      <div className="modal-actions">
        {result.failed_rows > 0 && <button className="button button-light" type="button" onClick={() => void errorsCsv()}>تنزيل تقرير الأخطاء (CSV)</button>}
        <button className="button button-dark" type="button" onClick={onDone}>عرض في سجل الاستيراد</button>
      </div>
    </section>}
  </>;
}

function ImportHistory({ onError }: { onError: (v: string) => void }) {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [errors, setErrors] = useState<{ row_number: number; reason: string }[]>([]);
  useEffect(() => {
    const load = async () => {
      try { setBatches((await api<{ imports: Batch[] }>('/api/crm/imports')).imports); }
      catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل السجل.'); }
    };
    void load();
  }, [onError]);
  const open = async (batch: Batch) => {
    try { setErrors((await api<Batch & { errors: { row_number: number; reason: string }[] }>(`/api/crm/imports/${batch.id}`)).errors); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر فتح الدفعة.'); }
  };
  return <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr><th>الملف</th><th>التاريخ</th><th>الإجمالي</th><th>ناجح</th><th>فشل</th><th>الحالة</th><th></th></tr></thead><tbody>
    {batches.map((batch) => <tr key={batch.id}><td>{batch.file_name}</td><td>{formatDateTime(batch.created_at)}</td><td>{batch.total_rows}</td><td>{batch.success_rows}</td><td>{batch.failed_rows}</td><td>{batch.status}</td>
      <td><button className="button button-light button-small" type="button" onClick={() => void open(batch)}>الأخطاء</button></td></tr>)}
    {!batches.length && <tr><td colSpan={7}><div className="empty-inline">لا عمليات استيراد بعد.</div></td></tr>}
  </tbody></table></div>
  {errors.length > 0 && <div><h4>أخطاء الدفعة المختارة</h4>{errors.map((error, index) => <p key={index}>صف {error.row_number}: {error.reason}</p>)}</div>}
  </section>;
}
