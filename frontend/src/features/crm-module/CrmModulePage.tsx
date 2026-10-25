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
 * Isolated CRM module: Dashboard / Leads / Import Leads / Import History
 * over `/api/crm` (crm_* tables). The sidebar group is permission-gated in
 * StaffConsole with the same `crm.view` check repeated here.
 */
export type CrmModuleTab = 'dashboard' | 'leads' | 'import' | 'history';

interface Lead { id: string; full_name: string; phone: string | null; email: string | null; source: string; status: string; assigned_to: string | null; notes: string | null; created_at: string; activities?: Activity[] }
interface Activity { id: string; type: string; description: string; created_at: string }
interface LeadsPage { items: Lead[]; total: number; page: number; page_size: number; pages: number }
interface Batch { id: string; file_name: string; total_rows: number; success_rows: number; failed_rows: number; status: string; created_at: string }
interface Stats { total_leads: number; by_status: Record<string, number>; by_source: Record<string, number>; last_30_days: { date: string; count: number }[]; conversion_rate: number; recent_leads: Lead[]; recent_imports: Batch[]; recent_activities: Activity[] }
interface Preview { columns: string[]; total_rows: number; valid_rows: number; invalid_rows: number; preview: Record<string, string>[]; errors: { row_number: number; reason: string }[] }
interface ImportResult { batch_id: string; total_rows: number; success_rows: number; failed_rows: number; duplicates: number; status: string }

const STATUS_LABELS: Record<string, string> = { new: 'جديد', contacted: 'تم التواصل', qualified: 'مؤهل', converted: 'محوّل', lost: 'خاسر' };
const SOURCE_LABELS: Record<string, string> = { manual: 'يدوي', whatsapp: 'واتساب', call: 'مكالمة', email: 'إيميل', portal: 'البوابة', import: 'استيراد', other: 'أخرى' };

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

export function CrmModulePage({ access, tab, onTab }: { access: StaffAccess; tab: CrmModuleTab; onTab: (tab: CrmModuleTab) => void }) {
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const canView = hasPermission(access, 'crm.view');
  const canManage = hasPermission(access, 'crm.manage');

  if (!canView) return <AccessDenied title="CRM غير متاح" />;
  return <div className="page-stack">
    {error && <div className="inline-alert error" role="alert">{error}</div>}
    {notice && <div className="inline-alert" role="status">{notice}</div>}
    {tab === 'dashboard' && <Dashboard onError={setError} />}
    {tab === 'leads' && <Leads access={access} canManage={canManage} onError={setError} onNotice={setNotice} />}
    {tab === 'import' && <ImportLeads canManage={canManage} onError={setError} onNotice={setNotice} onDone={() => onTab('history')} />}
    {tab === 'history' && <ImportHistory onError={setError} />}
  </div>;
}

function Dashboard({ onError }: { onError: (value: string) => void }) {
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    const load = async () => {
      try { setStats(await api<Stats>('/api/crm/stats')); }
      catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل إحصاءات CRM.'); }
    };
    void load();
  }, [onError]);
  if (!stats) return <div className="loading-surface"><span className="loading-spinner" /> جارٍ تحميل لوحة CRM…</div>;
  const statusData = Object.entries(STATUS_LABELS).map(([key, label]) => ({ name: label, count: stats.by_status[key] ?? 0 }));
  const sourceData = Object.entries(stats.by_source).map(([key, count]) => ({ name: SOURCE_LABELS[key] ?? key, count }));
  return <>
    <section className="surface-card user-summary-strip">
      <div className="summary-stat"><div><strong>{formatNumber(stats.total_leads)}</strong><small>إجمالي العملاء المحتملين</small></div></div>
      <div className="summary-stat"><div><strong>{stats.conversion_rate}٪</strong><small>معدل التحويل</small></div></div>
      <div className="summary-stat"><div><strong>{formatNumber(stats.last_30_days.reduce((sum, day) => sum + day.count, 0))}</strong><small>جدد آخر ٣٠ يوماً</small></div></div>
      <div className="summary-stat"><div><strong>{formatNumber(stats.recent_imports.length)}</strong><small>عمليات استيراد أخيرة</small></div></div>
    </section>
    <div className="crmm-grid">
      <section className="surface-card"><h3>حسب الحالة</h3><ResponsiveContainer width="100%" height={220}><BarChart data={statusData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="name" /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#0f2a52" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer></section>
      <section className="surface-card"><h3>حسب المصدر</h3><ResponsiveContainer width="100%" height={220}><BarChart data={sourceData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="name" /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#b8912f" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer></section>
      <section className="surface-card"><h3>الجدد آخر ٣٠ يوماً</h3><ResponsiveContainer width="100%" height={220}><BarChart data={stats.last_30_days}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="date" hide /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="count" fill="#1d7a5f" /></BarChart></ResponsiveContainer></section>
    </div>
    <div className="crmm-grid">
      <section className="surface-card"><h3>أحدث العملاء المحتملين</h3>{stats.recent_leads.map((lead) => <p key={lead.id}>{lead.full_name} · {STATUS_LABELS[lead.status] ?? lead.status} · <span dir="ltr">{lead.phone ?? ''}</span></p>)}{!stats.recent_leads.length && <div className="empty-inline">لا بيانات بعد — استورد ملف Excel للبدء.</div>}</section>
      <section className="surface-card"><h3>أحدث الاستيرادات</h3>{stats.recent_imports.map((batch) => <p key={batch.id}>{batch.file_name} · ناجح {batch.success_rows}/{batch.total_rows} · {batch.status}</p>)}</section>
      <section className="surface-card"><h3>أحدث الأنشطة</h3>{stats.recent_activities.map((activity) => <p key={activity.id}>{activity.type}: {activity.description} <small>({formatDateTime(activity.created_at)})</small></p>)}</section>
    </div>
  </>;
}

function Leads({ access, canManage, onError, onNotice }: { access: StaffAccess; canManage: boolean; onError: (v: string) => void; onNotice: (v: string) => void }) {
  const [data, setData] = useState<LeadsPage | null>(null);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState('created_at');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<Lead | null>(null);
  const [agents, setAgents] = useState<{ id: string; full_name: string }[]>([]);
  const [bulkAssignee, setBulkAssignee] = useState('');

  const load = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page), page_size: '20', sort, order });
    if (search) query.set('search', search);
    if (status) query.set('status', status);
    if (source) query.set('source', source);
    try { setData(await api<LeadsPage>(`/api/crm/leads?${query}`)); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تحميل العملاء المحتملين.'); }
  }, [search, status, source, page, sort, order, onError]);

  useEffect(() => { const run = async () => { await load(); }; void run(); }, [load]);
  useEffect(() => {
    const run = async () => {
      try {
        const users = await api<{ items?: { id: string; full_name: string }[] }>('/api/staff/users');
        setAgents(users.items ?? []);
      } catch { setAgents([{ id: access.id, full_name: access.full_name }]); }
    };
    void run();
  }, [access.id, access.full_name]);

  const toggleSort = (field: string) => { if (sort === field) setOrder((value) => (value === 'asc' ? 'desc' : 'asc')); else { setSort(field); setOrder('asc'); } };
  const toggle = (id: string) => setSelected((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const bulk = async (path: string, body: Record<string, unknown>, message: string) => {
    try { await api(path, { method: 'POST', body: jsonBody(body) }); onNotice(message); setSelected(new Set()); await load(); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر تنفيذ العملية.'); }
  };
  const openLead = async (id: string) => { try { setDetail(await api<Lead>(`/api/crm/leads/${id}`)); } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الفتح.'); } };
  const remove = async (lead: Lead) => {
    if (!window.confirm(`حذف «${lead.full_name}» نهائياً؟`)) return;
    try { await api(`/api/crm/leads/${lead.id}`, { method: 'DELETE' }); onNotice('تم حذف العميل المحتمل.'); await load(); }
    catch (err) { onError(err instanceof Error ? err.message : 'تعذر الحذف.'); }
  };
  const createLead = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api('/api/crm/leads', { method: 'POST', body: jsonBody({ full_name: form.get('full_name'), phone: form.get('phone') || null, email: form.get('email') || null, source: form.get('source'), notes: form.get('notes') || null }) });
      onNotice('تمت إضافة العميل المحتمل.'); event.currentTarget.reset(); await load();
    } catch (err) { onError(err instanceof Error ? err.message : 'تعذر الإضافة.'); }
  };

  return <>
    {canManage && <section className="surface-card"><h3>إضافة Lead</h3><form className="crmm-toolbar" onSubmit={createLead}>
      <label className="field-label">الاسم الكامل<input name="full_name" required minLength={2} /></label>
      <label className="field-label">الهاتف<input name="phone" dir="ltr" placeholder="010…" /></label>
      <label className="field-label">البريد<input name="email" type="email" dir="ltr" /></label>
      <label className="field-label">المصدر<select name="source">{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">ملاحظات<input name="notes" /></label>
      <button className="button button-dark" type="submit">إضافة</button>
    </form></section>}
    <section className="surface-card"><div className="crmm-toolbar">
      <label className="field-label">بحث<input value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} placeholder="اسم / هاتف / بريد" /></label>
      <label className="field-label">الحالة<select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}><option value="">الكل</option>{Object.entries(STATUS_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">المصدر<select value={source} onChange={(e) => { setSource(e.target.value); setPage(1); }}><option value="">الكل</option>{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      {canManage && selected.size > 0 && <>
        <label className="field-label">إسناد جماعي<select value={bulkAssignee} onChange={(e) => setBulkAssignee(e.target.value)}><option value="">— إلغاء الإسناد —</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
        <button className="button button-light" type="button" onClick={() => void bulk('/api/crm/leads/bulk-assign', { ids: [...selected], assigned_to: bulkAssignee || null }, 'تم الإسناد الجماعي.')}>إسناد ({selected.size})</button>
        <button className="button button-light" type="button" onClick={() => void bulk('/api/crm/leads/bulk-status', { ids: [...selected], status: 'contacted' }, 'تم تغيير الحاله.')} >تعليم «تم التواصل»</button>
      </>}
    </div></section>
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr>
      <th></th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('full_name')}>الاسم</button></th>
      <th>الهاتف</th><th>البريد</th><th>المصدر</th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('status')}>الحالة</button></th>
      <th><button type="button" className="link-button" onClick={() => toggleSort('created_at')}>التاريخ</button></th><th></th>
    </tr></thead><tbody>
      {(data?.items ?? []).map((lead) => <tr key={lead.id}>
        <td><input type="checkbox" aria-label={`تحديد ${lead.full_name}`} checked={selected.has(lead.id)} onChange={() => toggle(lead.id)} /></td>
        <td><button type="button" className="link-button" onClick={() => void openLead(lead.id)}><strong>{lead.full_name}</strong></button></td>
        <td dir="ltr">{lead.phone ?? '—'}</td><td dir="ltr">{lead.email ?? '—'}</td>
        <td>{SOURCE_LABELS[lead.source] ?? lead.source}</td><td>{STATUS_LABELS[lead.status] ?? lead.status}</td>
        <td>{formatDateTime(lead.created_at)}</td>
        <td>{canManage && <button type="button" className="button button-light button-small" onClick={() => void remove(lead)}>حذف</button>}</td>
      </tr>)}
      {data && !data.items.length && <tr><td colSpan={8}><div className="empty-inline">لا يوجد عملاء محتملون مطابقون.</div></td></tr>}
    </tbody></table></div>
      <div className="crmm-pager">
        <button className="button button-light button-small" type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>السابق</button>
        <span>صفحة {data?.page ?? page} من {data?.pages ?? 0} · {formatNumber(data?.total ?? 0)}</span>
        <button className="button button-light button-small" type="button" disabled={!data || page >= data.pages} onClick={() => setPage((p) => p + 1)}>التالي</button>
      </div>
    </section>
    {detail && <LeadDrawer lead={detail} canManage={canManage} agents={agents} onClose={() => setDetail(null)} onChanged={async () => { await openLead(detail.id); await load(); }} onError={onError} />}
  </>;
}

function LeadDrawer({ lead, canManage, agents, onClose, onChanged, onError }: { lead: Lead; canManage: boolean; agents: { id: string; full_name: string }[]; onClose: () => void; onChanged: () => Promise<void>; onError: (v: string) => void }) {
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api(`/api/crm/leads/${lead.id}`, { method: 'PATCH', body: jsonBody({ full_name: form.get('full_name'), phone: form.get('phone') || null, email: form.get('email') || null, status: form.get('status'), source: form.get('source'), assigned_to: form.get('assigned_to') || null, notes: form.get('notes') || null }) });
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
  return <Modal title={lead.full_name} eyebrow="تفاصيل Lead" onClose={onClose} wide>
    {canManage ? <form className="crmm-toolbar" onSubmit={save}>
      <label className="field-label">الاسم<input name="full_name" defaultValue={lead.full_name} required /></label>
      <label className="field-label">الهاتف<input name="phone" dir="ltr" defaultValue={lead.phone ?? ''} /></label>
      <label className="field-label">البريد<input name="email" dir="ltr" defaultValue={lead.email ?? ''} /></label>
      <label className="field-label">الحالة<select name="status" defaultValue={lead.status}>{Object.entries(STATUS_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">المصدر<select name="source" defaultValue={lead.source}>{Object.entries(SOURCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="field-label">المسؤول<select name="assigned_to" defaultValue={lead.assigned_to ?? ''}><option value="">غير مسند</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.full_name}</option>)}</select></label>
      <label className="field-label">ملاحظات<input name="notes" defaultValue={lead.notes ?? ''} /></label>
      <button className="button button-dark" type="submit">حفظ</button>
    </form> : <p>{lead.phone ?? ''} · {lead.email ?? ''}</p>}
    <h4>سجل الأنشطة</h4>
    <ul className="crmm-timeline">{(lead.activities ?? []).map((activity) => <li key={activity.id}><strong>{activity.type}</strong> — {activity.description}<br /><small>{formatDateTime(activity.created_at)}</small></li>)}</ul>
    {canManage && <form className="crmm-toolbar" onSubmit={addActivity}>
      <label className="field-label">النوع<select name="type"><option value="call">مكالمة</option><option value="note">ملاحظة</option><option value="whatsapp">واتساب</option><option value="email">إيميل</option><option value="meeting">اجتماع</option></select></label>
      <label className="field-label">الوصف<input name="description" required /></label>
      <button className="button button-light" type="submit">تسجيل نشاط</button>
    </form>}
  </Modal>;
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
      <small className="muted">الأعمدة: full_name (الاسم)، phone (الهاتف)، email (البريد)، source (المصدر)، notes (ملاحظات). تُطوى أرقام 01… إلى ‎+20‎ وتُعلَّم الصفوف المكررة/غير الصالحة كأخطاء بدل إدخالها.</small>
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
