import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Modal } from '../../components/Modal';
import { hasPermission } from '../../utils/access';
import type { StaffAccess } from '../../types';
import './insurance-ops.css';

/**
 * Insurance operations workspace: issuing files, policy lifecycle
 * (installments, ~5% commission, renewals), corporate Excel imports,
 * the staff Client Folder, caller lookup and the insurer-performance
 * report. Sales CRM lives only in the unified CRM module
 * (`/staff/crm-module`, `/api/crm`).
 */
interface CrmLeadOption { id: string; lead_no: string; full_name: string; company_name: string | null }
interface Policy { id: string; corporate_client_id: string; insurer_company_id: string; policy_no: string; insurance_type: string; starts_on: string; ends_on: string; premium: string; installment_plan: string; commission_amount: string; status: string }
interface PolicyDetail extends Policy { installments: { id: string; sequence_no: number; due_date: string; amount: string; status: string }[]; settlements: { id: string; expected_amount: string; received_amount: string; status: string }[] }
interface Issuing { id: string; insurance_type: string; status: string; insurer_reference: string | null; checklist: { id: string; document_name: string; is_required: boolean; status: string }[] }
interface ImportJob { id: string; kind: string; filename: string; rows_total: number; rows_succeeded: number; rows_failed: number; created_at: string }
interface Company { id: string; name_en: string; name_ar: string }
interface ClientOption { id: string; name_ar?: string; name_en?: string; slug?: string }
interface ClientFolder { client: Record<string, string>; contracts: Record<string, string>[]; documents: Record<string, string>[]; employees: Record<string, string>[]; tickets_by_status: Record<string, number>; policies: Policy[] }

const PLAN_LABELS: Record<string, string> = { monthly: 'شهري', quarterly: 'ربع سنوي', semi_annual: 'نصف سنوي', cash: 'كاش' };

type Tab = 'policies' | 'issuing' | 'imports' | 'folder' | 'reports';

export function InsuranceOpsPage({ access }: { access: StaffAccess }) {
  const [pendingFolderId] = useState(() => window.localStorage.getItem('brokerhub:open-client-folder') ?? '');
  const [tab, setTab] = useState<Tab>(pendingFolderId ? 'folder' : 'policies');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [leads, setLeads] = useState<CrmLeadOption[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [renewals, setRenewals] = useState<Policy[]>([]);
  const [policyDetail, setPolicyDetail] = useState<PolicyDetail | null>(null);
  const [issuing, setIssuing] = useState<Issuing[]>([]);
  const [imports, setImports] = useState<ImportJob[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [clients, setClients] = useState<ClientOption[]>([]);
  const [folder, setFolder] = useState<ClientFolder | null>(null);
  const [folderClientId, setFolderClientId] = useState(pendingFolderId);
  const [insurerRows, setInsurerRows] = useState<Record<string, string | number | null>[]>([]);
  const [searchQ, setSearchQ] = useState('');
  const [searchResult, setSearchResult] = useState('');
  const [callerMobile, setCallerMobile] = useState('');
  const [callerResult, setCallerResult] = useState('');

  const canCrm = hasPermission(access, 'crm.view');
  const canFolderAccess = canCrm || hasPermission(access, 'corporate.view');
  const canPolicies = hasPermission(access, 'policies.view');
  const canManagePolicies = hasPermission(access, 'policies.manage');
  const canIssuing = hasPermission(access, 'issuing.view');
  const canManageIssuing = hasPermission(access, 'issuing.manage');
  const canImport = hasPermission(access, 'imports.manage');
  const canPay = hasPermission(access, 'payments.manage');

  const load = useCallback(async () => {
    const safe = async <T,>(call: Promise<T>, fallback: T): Promise<T> => { try { return await call; } catch { return fallback; } };
    const [ld, po, rn, is, im, co, cl, ip] = await Promise.all([
      safe(api<{ items: CrmLeadOption[] }>('/api/crm/leads?page_size=100'), { items: [] }),
      safe(api<{ policies: Policy[] }>('/api/staff/insurance-ops/policies'), { policies: [] }),
      safe(api<{ renewals: Policy[] }>('/api/staff/insurance-ops/renewals?days=30'), { renewals: [] }),
      safe(api<{ issuing: Issuing[] }>('/api/staff/insurance-ops/issuing'), { issuing: [] }),
      safe(api<{ imports: ImportJob[] }>('/api/staff/insurance-ops/imports'), { imports: [] }),
      safe(api<Company[]>('/api/companies'), []),
      safe(api<{ items: ClientOption[] }>('/api/staff/corporate/sales-directory'), { items: [] }),
      safe(api<{ insurers: Record<string, string | number | null>[] }>('/api/staff/insurance-ops/insurer-performance'), { insurers: [] }),
    ]);
    setLeads(ld.items); setPolicies(po.policies);
    setRenewals(rn.renewals); setIssuing(is.issuing); setImports(im.imports);
    setCompanies(co); setClients(cl.items ?? []); setInsurerRows(ip.insurers);
  }, []);

  useEffect(() => {
    const initial = async () => { if (canFolderAccess) await load(); };
    void initial();
  }, [canFolderAccess, load]);

  useEffect(() => {
    if (!pendingFolderId) return;
    window.localStorage.removeItem('brokerhub:open-client-folder');
    void api<ClientFolder>(`/api/staff/insurance-ops/client-folder/${pendingFolderId}`)
      .then((result) => { setFolder(result); setFolderClientId(pendingFolderId); setTab('folder'); setError(''); })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'تعذر فتح ملف العميل.'));
  }, [pendingFolderId]);

  if (!canFolderAccess) return <AccessDenied title="ملف العميل غير متاح" />;

  const run = async (call: () => Promise<unknown>, message: string) => {
    setError(''); setNotice('');
    try { await call(); setNotice(message); await load(); } catch (err) { setError(err instanceof Error ? err.message : 'تعذر تنفيذ العملية.'); }
  };

  const createPolicy = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(() => api('/api/staff/insurance-ops/policies', {
      method: 'POST',
      body: jsonBody({
        corporate_client_id: form.get('corporate_client_id'), insurer_company_id: form.get('insurer_company_id'),
        policy_no: form.get('policy_no'), insurance_type: form.get('insurance_type'),
        starts_on: form.get('starts_on'), ends_on: form.get('ends_on'), premium: form.get('premium'),
        installment_plan: form.get('installment_plan'), commission_rate: form.get('commission_rate') || '0.05',
      }),
    }), 'تم إنشاء الوثيقة وجدول أقساطها وتسوية عمولتها (٥٪ افتراضياً).');
    event.currentTarget.reset();
  };

  const createIssuing = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(() => api('/api/staff/insurance-ops/issuing', {
      method: 'POST',
      body: jsonBody({ insurer_company_id: form.get('insurer_company_id'), insurance_type: form.get('insurance_type'), lead_id: form.get('lead_id') || null, corporate_client_id: form.get('corporate_client_id') || null }),
    }), 'تم فتح ملف الإصدار مع قائمة الأوراق المطلوبة.');
    event.currentTarget.reset();
  };

  const uploadImport = (kind: 'employees' | 'clients') => (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(() => api(`/api/staff/insurance-ops/imports/${kind}`, { method: 'POST', body: form }), 'اكتمل الاستيراد — راجع نتيجة كل صف في سجل الاستيراد.');
    event.currentTarget.reset();
  };

  const openFolder = async () => {
    if (!folderClientId) return;
    try { setFolder(await api<ClientFolder>(`/api/staff/insurance-ops/client-folder/${folderClientId}`)); setError(''); }
    catch (err) { setError(err instanceof Error ? err.message : 'تعذر فتح ملف العميل.'); }
  };

  const doSearch = async () => {
    try {
      const result = await api<{ documents: unknown[]; policies: unknown[]; tickets: unknown[] }>(`/api/staff/insurance-ops/document-search?q=${encodeURIComponent(searchQ)}`);
      setSearchResult(`وثائق تعاقد: ${result.documents.length} · وثائق تأمين: ${result.policies.length} · طلبات: ${result.tickets.length}`);
    } catch (err) { setError(err instanceof Error ? err.message : 'تعذر البحث.'); }
  };

  const doCaller = async () => {
    try {
      const result = await api<{ caller: { full_name?: string; corporate_name_ar?: string; policy_number?: string } | null }>(`/api/staff/insurance-ops/caller-lookup?mobile=${encodeURIComponent(callerMobile)}`);
      setCallerResult(result.caller ? `${result.caller.full_name ?? '—'} · ${result.caller.corporate_name_ar ?? 'بدون شركة'} · وثيقة ${result.caller.policy_number ?? '—'}` : 'لا يوجد موظف مسجل بهذا الرقم.');
    } catch (err) { setError(err instanceof Error ? err.message : 'تعذر التعرف على المتصل.'); }
  };

  const tabs: { id: Tab; label: string }[] = [
    ...(canPolicies ? [{ id: 'policies' as Tab, label: 'الوثائق والأقساط' }] : []),
    ...(canIssuing ? [{ id: 'issuing' as Tab, label: 'الإصدار' }] : []),
    ...(canImport ? [{ id: 'imports' as Tab, label: 'استيراد Excel' }] : []),
    { id: 'folder', label: 'ملف العميل' },
    { id: 'reports', label: 'تقارير الأداء' },
  ];

  return <div className="page-stack">
    {error && <div className="inline-alert error" role="alert">{error}</div>}
    {notice && <div className="inline-alert" role="status">{notice}</div>}
    <div className="iops-tabs" role="tablist">{tabs.map((item) => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>{item.label}</button>)}</div>

    {tab === 'policies' && canPolicies && <>
      <section className="surface-card"><h3>تنبيهات التجديد (خلال ٣٠ يوماً)</h3>
        {renewals.length ? renewals.map((policy) => <p key={policy.id}>{policy.policy_no} — تنتهي {policy.ends_on}</p>) : <div className="empty-inline">لا توجد وثائق على وشك الانتهاء.</div>}</section>
      {canManagePolicies && <section className="surface-card"><h3>وثيقة جديدة</h3>
        <form className="iops-form-grid" onSubmit={createPolicy}>
          <label className="field-label">الشركة العميلة<select name="corporate_client_id" required>{clients.map((client) => <option key={client.id} value={client.id}>{client.name_ar ?? client.name_en ?? client.slug}</option>)}</select></label>
          <label className="field-label">شركة التأمين<select name="insurer_company_id" required>{companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar}</option>)}</select></label>
          <label className="field-label">رقم الوثيقة<input name="policy_no" required minLength={3} /></label>
          <label className="field-label">النوع<select name="insurance_type"><option value="health">طبي</option><option value="general">عام</option><option value="both">كلاهما</option></select></label>
          <label className="field-label">من<input name="starts_on" type="date" required /></label>
          <label className="field-label">إلى<input name="ends_on" type="date" required /></label>
          <label className="field-label">القسط<input name="premium" type="number" min="1" step="0.01" required /></label>
          <label className="field-label">نظام الأقساط<select name="installment_plan">{Object.entries(PLAN_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="field-label">نسبة العمولة<input name="commission_rate" type="number" min="0" max="1" step="0.001" defaultValue="0.05" /></label>
          <button className="button button-dark" type="submit">إنشاء الوثيقة</button>
        </form></section>}
      <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table"><thead><tr><th>الوثيقة</th><th>الفترة</th><th>القسط</th><th>النظام</th><th>العمولة</th><th>الحالة</th></tr></thead><tbody>
        {policies.map((policy) => <tr key={policy.id} onClick={() => { void api<PolicyDetail>(`/api/staff/insurance-ops/policies/${policy.id}`).then(setPolicyDetail).catch(() => undefined); }}><td>{policy.policy_no}</td><td>{policy.starts_on} ← {policy.ends_on}</td><td>{policy.premium}</td><td>{PLAN_LABELS[policy.installment_plan] ?? policy.installment_plan}</td><td>{policy.commission_amount}</td><td>{policy.status}</td></tr>)}
        {!policies.length && <tr><td colSpan={6}><div className="empty-inline">لا توجد وثائق بعد.</div></td></tr>}
      </tbody></table></div></section>
      {policyDetail && <Modal title={`وثيقة ${policyDetail.policy_no}`} eyebrow="الأقساط وتسوية العمولة" onClose={() => setPolicyDetail(null)}>
        <h4>الأقساط</h4>{policyDetail.installments.map((item) => <p key={item.id}>قسط {item.sequence_no}: {item.amount} — يستحق {item.due_date} — {item.status} {canPay && item.status !== 'paid' && <button className="button button-light button-small" type="button" onClick={() => void run(async () => { await api(`/api/staff/insurance-ops/installments/${item.id}/pay`, { method: 'POST', body: jsonBody({ provider: 'manual' }) }); setPolicyDetail(await api<PolicyDetail>(`/api/staff/insurance-ops/policies/${policyDetail.id}`)); }, 'تم تسجيل دفع القسط.')}>تسجيل دفع</button>}</p>)}
        <h4>تسوية العمولة</h4>{policyDetail.settlements.map((item) => <p key={item.id}>متوقع {item.expected_amount} · مستلم {item.received_amount} · {item.status}</p>)}
      </Modal>}
    </>}

    {tab === 'issuing' && canIssuing && <>
      {canManageIssuing && <section className="surface-card"><h3>ملف إصدار جديد</h3>
        <form className="iops-form-grid" onSubmit={createIssuing}>
          <label className="field-label">شركة التأمين<select name="insurer_company_id" required>{companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar}</option>)}</select></label>
          <label className="field-label">النوع<select name="insurance_type"><option value="health">طبي</option><option value="general">عام</option><option value="both">كلاهما</option></select></label>
          <label className="field-label">عميل محتمل (اختياري)<select name="lead_id"><option value="">—</option>{leads.map((lead) => <option key={lead.id} value={lead.id}>{lead.company_name ?? lead.full_name} · {lead.lead_no}</option>)}</select></label>
          <button className="button button-dark" type="submit">فتح الملف</button>
        </form></section>}
      {issuing.map((item) => <section className="surface-card" key={item.id}><h4>إصدار {item.insurance_type} — {item.status}</h4>
        {item.checklist.map((doc) => <p key={doc.id}>{doc.document_name}{doc.is_required ? ' *' : ''} — {doc.status} {canManageIssuing && doc.status === 'pending' && <button className="button button-light button-small" type="button" onClick={() => void run(() => api(`/api/staff/insurance-ops/issuing/items/${doc.id}`, { method: 'PATCH', body: jsonBody({ status: 'received' }) }), 'تم استلام الورقة.')}>استلام</button>}</p>)}
        {canManageIssuing && <div className="modal-actions">
          {item.status === 'draft' && <button className="button button-light" type="button" onClick={() => void run(() => api(`/api/staff/insurance-ops/issuing/${item.id}`, { method: 'PATCH', body: jsonBody({ status: 'internal_review' }) }), 'أُرسل للمراجعة الداخلية.')}>مراجعة داخلية</button>}
          {item.status === 'internal_review' && <button className="button button-light" type="button" onClick={() => void run(() => api(`/api/staff/insurance-ops/issuing/${item.id}`, { method: 'PATCH', body: jsonBody({ status: 'sent_to_insurer' }) }), 'أُرسل لشركة التأمين.')}>إرسال لشركة التأمين</button>}
          {item.status === 'sent_to_insurer' && <button className="button button-dark" type="button" onClick={() => void run(() => api(`/api/staff/insurance-ops/issuing/${item.id}`, { method: 'PATCH', body: jsonBody({ status: 'issued' }) }), 'تم إصدار الوثيقة.')}>تم الإصدار</button>}
        </div>}</section>)}
      {!issuing.length && <div className="empty-inline">لا توجد ملفات إصدار.</div>}
    </>}

    {tab === 'imports' && canImport && <div className="iops-split">
      <section className="surface-card"><h3>استيراد الموظفين المؤمّن عليهم</h3><form onSubmit={uploadImport('employees')} className="iops-form-grid"><label className="field-label">الشركة العميلة<select name="corporate_client_id" required>{clients.map((client) => <option key={client.id} value={client.id}>{client.name_ar ?? client.name_en ?? client.slug}</option>)}</select></label><label className="field-label">ملف Excel/CSV<input name="file" type="file" accept=".xlsx,.csv" required /></label><button className="button button-dark" type="submit">استيراد</button></form><small className="muted">الأعمدة: full_name/full_name، mobile، email، national_id (اختياري)، employee_code.</small></section>
      <section className="surface-card"><h3>استيراد العملاء الحاليين</h3><form onSubmit={uploadImport('clients')} className="iops-form-grid"><label className="field-label">ملف Excel/CSV<input name="file" type="file" accept=".xlsx,.csv" required /></label><button className="button button-dark" type="submit">استيراد</button></form></section>
      <section className="surface-card"><h3>سجل الاستيراد</h3>{imports.map((job) => <p key={job.id}>{job.filename} · {job.kind} · ناجح {job.rows_succeeded}/{job.rows_total} · فشل {job.rows_failed}</p>)}{!imports.length && <div className="empty-inline">لا عمليات استيراد بعد.</div>}</section>
    </div>}

    {tab === 'folder' && <>
      <section className="surface-card"><h3>ملف العميل (Client Folder)</h3>
        <div className="iops-form-grid"><label className="field-label">الشركة العميلة<select value={folderClientId} onChange={(e) => setFolderClientId(e.target.value)}><option value="">اختر…</option>{clients.map((client) => <option key={client.id} value={client.id}>{client.name_ar ?? client.name_en ?? client.slug}</option>)}</select></label>
        <button className="button button-dark" type="button" onClick={() => void openFolder()}>فتح الملف</button></div></section>
      {folder && <div className="iops-split">
        <section className="surface-card"><h4>العقود</h4>{folder.contracts.map((c, i) => <p key={i}>{c.contract_no} · {c.contract_value} {c.currency} · {c.payment_frequency} · تجديد {c.renews_on}</p>)}</section>
        <section className="surface-card"><h4>الوثائق المرفوعة</h4>{folder.documents.map((d, i) => <p key={i}>{d.title} ({d.doc_kind})</p>)}</section>
        <section className="surface-card"><h4>الموظفون المؤمّن عليهم ({folder.employees.length})</h4>{folder.employees.slice(0, 20).map((e, i) => <p key={i}>{e.full_name} · <span dir="ltr">{e.mobile}</span></p>)}</section>
        <section className="surface-card"><h4>طلبات الشركة</h4>{Object.entries(folder.tickets_by_status).map(([status, n]) => <p key={status}>{status}: {n}</p>)}<h4>وثائق التأمين</h4>{folder.policies.map((p) => <p key={p.id}>{p.policy_no} — {p.premium}</p>)}</section>
      </div>}
    </>}

    {tab === 'reports' && <div className="iops-split">
      <section className="surface-card"><h3>أداء شركات التأمين</h3>{insurerRows.map((row, i) => <p key={i}>{String(row.insurer_name_ar ?? row.insurer_name_en)} · طلبات {String(row.requests)} · تم حلها {String(row.resolved)} · تجاوز SLA ‏{String(row.sla_breaches)} · متوسط الرد/الحل (ساعة) {String(row.avg_resolution_hours ?? '—')}</p>)}</section>
      <section className="surface-card"><h3>بحث ذكي في الوثائق</h3><div className="iops-form-grid"><label className="field-label">كلمة البحث<input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} /></label><button className="button button-dark" type="button" onClick={() => void doSearch()}>بحث</button></div>{searchResult && <p>{searchResult}</p>}</section>
      <section className="surface-card"><h3>التعرف على المتصل</h3><div className="iops-form-grid"><label className="field-label">رقم الموبايل<input value={callerMobile} dir="ltr" onChange={(e) => setCallerMobile(e.target.value)} /></label><button className="button button-dark" type="button" onClick={() => void doCaller()}>تعريف</button></div>{callerResult && <p>{callerResult}</p>}</section>
    </div>}

  </div>;
}
