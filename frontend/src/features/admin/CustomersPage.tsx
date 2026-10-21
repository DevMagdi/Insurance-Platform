import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { hasPermission } from '../../utils/access';
import { formatDateTime, formatNumber } from '../../utils/date';
import type { StaffAccess } from '../../types';

interface Company { id: string; name_en: string; name_ar: string; }
interface Policy { company_name_en: string; company_name_ar: string; insurance_type: string; policy_number: string; effective_from: string | null; expires_at: string | null; }
interface CustomerComplaint { ticket_no: string; subject: string; status: string; created_at: string; company_name_en: string; company_name_ar: string; insurance_type: string; }
interface Customer { id: string; full_name: string; email: string | null; mobile: string; whatsapp_available: boolean; created_at: string; national_id_last4?: string | null; companies: Company[]; policies: Policy[]; complaints: CustomerComplaint[]; complaints_count: number; }

export function CustomersPage({ access }: { access: StaffAccess }) {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Customer | null>(null);
  const [companyFilter, setCompanyFilter] = useState<string>('');
  const [hasPoliciesFilter, setHasPoliciesFilter] = useState<string>('');
  const [hasComplaintsFilter, setHasComplaintsFilter] = useState<string>('');
  const [dateFrom, setDateFrom] = useState<string>('');
  const [dateTo, setDateTo] = useState<string>('');
  const [reloadToken, setReloadToken] = useState(0);
  const [nationalId, setNationalId] = useState('');
  const [revealReason, setRevealReason] = useState('');
  const [revealBusy, setRevealBusy] = useState(false);
  const [revealError, setRevealError] = useState('');
  const [showRevealForm, setShowRevealForm] = useState(false);

  // Keep the directory live while the page is visible; no manual refresh.
  // A cheap revision signature is polled every 15s and the full directory is
  // only re-downloaded when it actually moved (new customer, new policy or a
  // new complaint), so a background tab costs one tiny aggregate per tick.
  const loadDirectory = useCallback(async () => {
    const [customersResult, companiesResult] = await Promise.allSettled([
      api<Customer[]>('/api/staff/customers'),
      api<Company[]>('/api/companies'),
    ]);
    const errors: string[] = [];
    if (customersResult.status === 'fulfilled') {
      setCustomers(customersResult.value);
    } else {
      errors.push(customersResult.reason instanceof Error ? customersResult.reason.message : 'تعذر تحميل العملاء.');
    }
    if (companiesResult.status === 'fulfilled') {
      setCompanies(companiesResult.value);
    } else {
      errors.push(companiesResult.reason instanceof Error ? companiesResult.reason.message : 'تعذر تحميل شركات التأمين.');
    }
    setError(errors.join(' '));
    setLoading(false);
  }, []);
  const loadDirectoryRef = useRef(loadDirectory);
  useEffect(() => { loadDirectoryRef.current = loadDirectory; }, [loadDirectory]);
  useEffect(() => {
    if (!hasPermission(access, 'users.view')) return;
    let previous = '';
    const poll = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        const data = await api<{ revision: string }>('/api/staff/customers/revision');
        const revision = data?.revision ?? '';
        if (!revision) return;
        if (previous && revision !== previous) void loadDirectoryRef.current();
        previous = revision;
      } catch { /* keep the current rows; the next tick retries */ }
    };
    void poll();
    const timer = window.setInterval(poll, 15_000);
    const onVisibility = () => { void poll(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [access]);

  const openCustomer = (customer: Customer) => {
    setNationalId('');
    setRevealReason('');
    setRevealError('');
    setShowRevealForm(false);
    setSelected(customer);
  };

  const canRevealNationalId = hasPermission(access, 'complaints.view_full_national_id');
  const revealNationalId = async () => {
    if (!selected || revealReason.trim().length < 10) return;
    setRevealBusy(true);
    setRevealError('');
    try {
      const result = await api<{ national_id: string }>(`/api/staff/customers/${encodeURIComponent(selected.id)}/national-id:reveal`, {
        method: 'POST', body: jsonBody({ reason: revealReason.trim() }),
      });
      setNationalId(result.national_id);
      setShowRevealForm(false);
    } catch (cause) {
      setRevealError(cause instanceof Error ? cause.message : 'تعذر إظهار رقم البطاقة.');
    } finally {
      setRevealBusy(false);
    }
  };

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void loadDirectory(); });
    return () => { active = false; };
  }, [loadDirectory, reloadToken]);

  // Load the insurer catalog independently of the current client directory so
  // companies with no associated clients are still available as filter options.
  const availableCompanies = useMemo(() => {
    const byId = new Map(companies.map((company) => [company.id, company]));
    // Keep historical insurers from existing client policies as well; the public
    // catalog intentionally contains active insurers only.
    customers.forEach((customer) => customer.companies.forEach((company) => {
      if (!byId.has(company.id)) byId.set(company.id, company);
    }));
    return [...byId.values()].sort(
      (a, b) => (a.name_ar || a.name_en).localeCompare(b.name_ar || b.name_en, 'ar'),
    );
  }, [companies, customers]);
  
  const filtered = useMemo(() => {
    let result = customers;
    const searchNeedle = search.trim().toLocaleLowerCase();
    if (searchNeedle) {
      result = result.filter((customer) => JSON.stringify(customer).toLocaleLowerCase().includes(searchNeedle));
    }
    if (companyFilter) {
      result = result.filter((customer) => customer.companies.some((company) => company.id === companyFilter));
    }
    if (hasPoliciesFilter === 'yes') {
      result = result.filter((customer) => customer.policies.length > 0);
    } else if (hasPoliciesFilter === 'no') {
      result = result.filter((customer) => customer.policies.length === 0);
    }
    if (hasComplaintsFilter === 'yes') {
      result = result.filter((customer) => customer.complaints_count > 0);
    } else if (hasComplaintsFilter === 'no') {
      result = result.filter((customer) => customer.complaints_count === 0);
    }
    if (dateFrom) {
      result = result.filter((customer) => customer.created_at.split('T')[0] >= dateFrom);
    }
    if (dateTo) {
      result = result.filter((customer) => customer.created_at.split('T')[0] <= dateTo);
    }
    return result;
  }, [customers, search, companyFilter, hasPoliciesFilter, hasComplaintsFilter, dateFrom, dateTo]);
  
  if (!hasPermission(access, 'users.view')) return <AccessDenied title="قائمة العملاء غير متاحة" />;

  return <div className="page-stack">
    <SectionTitle eyebrow="إدارة الهوية والوصول" title="العملاء" description="ملفات العملاء وشركات التأمين المرتبطة بوثائقهم؛ ابحث عبر كل بيانات الملف والسياسات." />
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { setLoading(true); setError(''); setReloadToken((value) => value + 1); }}>إعادة المحاولة</button></div>}
    <section className="surface-card user-summary-strip"><div className="summary-stat"><span className="summary-stat-icon"><Icon name="users" size={17} /></span><div><strong>{formatNumber(customers.length)}</strong><small>إجمالي العملاء</small></div></div><div className="summary-stat"><span className="summary-stat-icon summary-stat-green"><Icon name="building" size={17} /></span><div><strong>{formatNumber(new Set(customers.flatMap((customer) => customer.companies.map((company) => company.id))).size)}</strong><small>شركات مرتبطة</small></div></div><div className="summary-stat"><span className="summary-stat-icon summary-stat-gold"><Icon name="inbox" size={17} /></span><div><strong>{formatNumber(customers.reduce((count, customer) => count + customer.complaints_count, 0))}</strong><small>إجمالي الطلبات</small></div></div></section>
    
    <section className="surface-card admin-table-card" style={{ marginBottom: 16 }}>
      <div className="table-topline">
        <div><strong>فلاتر العملاء</strong></div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 16, padding: 16 }}>
        <div>
          <label className="field-label" style={{ display: 'block' }}>
            شركة التأمين
            <select value={companyFilter} onChange={(e) => setCompanyFilter(e.target.value)} style={{ width: '100%' }}>
              <option value="">كل الشركات</option>
              {availableCompanies.map((company) => (
                <option key={company.id} value={company.id}>{company.name_ar || company.name_en}</option>
              ))}
            </select>
          </label>
        </div>
        <div>
          <label className="field-label" style={{ display: 'block' }}>
            وجود وثائق
            <select value={hasPoliciesFilter} onChange={(e) => setHasPoliciesFilter(e.target.value)} style={{ width: '100%' }}>
              <option value="">كل العملاء</option>
              <option value="yes">لديهم وثائق</option>
              <option value="no">بدون وثائق</option>
            </select>
          </label>
        </div>
        <div>
          <label className="field-label" style={{ display: 'block' }}>
            وجود طلبات
            <select value={hasComplaintsFilter} onChange={(e) => setHasComplaintsFilter(e.target.value)} style={{ width: '100%' }}>
              <option value="">كل العملاء</option>
              <option value="yes">لديهم طلبات</option>
              <option value="no">بدون طلبات</option>
            </select>
          </label>
        </div>
        <div>
          <label className="field-label" style={{ display: 'block' }}>
            من تاريخ
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} style={{ width: '100%' }} />
          </label>
        </div>
        <div>
          <label className="field-label" style={{ display: 'block' }}>
            إلى تاريخ
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} style={{ width: '100%' }} />
          </label>
        </div>
      </div>
      <div style={{ padding: '0 16px 16px', display: 'flex', gap: 8 }}>
        <button className="button button-light button-small" type="button" onClick={() => { setSearch(''); setCompanyFilter(''); setHasPoliciesFilter(''); setHasComplaintsFilter(''); setDateFrom(''); setDateTo(''); }}>مسح الفلاتر</button>
      </div>
    </section>
    
    <div className="user-toolbar surface-card"><div className="queue-search-wrap"><Icon name="search" size={17} /><input aria-label="فلترة العملاء" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث في كل بيانات العميل والسياسات والشركات…" /></div><span className="muted small-copy">{formatNumber(filtered.length)} من {formatNumber(customers.length)}</span></div>
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table admin-users-table"><thead><tr><th>العميل</th><th>وسائل التواصل</th><th>شركة التأمين</th><th>الوثائق</th><th>الطلبات</th><th>تاريخ التسجيل</th></tr></thead><tbody>
      {loading && <tr><td colSpan={6}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل العملاء…</div></td></tr>}
      {!loading && filtered.map((customer) => {
        const companyNames = customer.companies.length 
          ? customer.companies.map((company) => company.name_ar || company.name_en).join('، ')
          : (customer.policies.length > 0 
              ? customer.policies.map((p) => p.company_name_ar || p.company_name_en).join('، ')
              : 'غير مرتبطة');
        return <tr key={customer.id} onClick={() => openCustomer(customer)} style={{ cursor: 'pointer' }} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') openCustomer(customer); }}>
          <td><button type="button" className="link-button" onClick={(event) => { event.stopPropagation(); openCustomer(customer); }}><strong>{customer.full_name}</strong></button><small className="cell-secondary" dir="ltr">{customer.id}</small></td>
          <td><span dir="ltr">{customer.mobile}</span><small className="cell-secondary" dir="ltr">{customer.email || '—'}</small></td>
          <td>{companyNames}</td>
          <td>{formatNumber(customer.policies.length)}</td>
          <td>{formatNumber(customer.complaints_count)}</td>
          <td>{formatDateTime(customer.created_at)}</td>
        </tr>;
      })}
      {!loading && !filtered.length && <tr><td colSpan={6}><div className="empty-table"><span className="empty-icon"><Icon name="users" size={20} /></span><strong>لا يوجد عملاء مطابقون</strong><span>ابحث بكلمات مختلفة تشمل الاسم أو رقم الوثيقة أو الشركة.</span></div></td></tr>}
    </tbody></table></div></section>
    {selected && <Modal title={selected.full_name} eyebrow="ملف العميل الكامل" onClose={() => setSelected(null)}><div className="page-stack customer-profile">
      <section className="surface-card customer-profile-section">
        <h3>الهوية</h3>
        <div className="customer-profile-grid">
          <div><span>الاسم الكامل</span><strong>{selected.full_name}</strong></div>
          <div><span>تاريخ التسجيل</span><strong>{formatDateTime(selected.created_at)}</strong></div>
        </div>
        <div className="customer-national-id">
          <span>رقم البطاقة</span>
          {nationalId
            ? <strong className="national-id-value" dir="ltr">{nationalId}</strong>
            : <>
              <strong className="national-id-value" dir="ltr">{selected.national_id_last4 ? `•••• •••• ${selected.national_id_last4}` : 'غير مسجل'}</strong>
              {canRevealNationalId && !showRevealForm && <button className="button button-light button-small" type="button" onClick={() => setShowRevealForm(true)}>إظهار رقم البطاقة</button>}
              {!canRevealNationalId && <small className="muted">الإظهار الكامل يتطلب صلاحية عرض رقم البطاقة.</small>}
            </>}
          {showRevealForm && !nationalId && <div className="customer-reveal-form">
            <label className="field-label">سبب الإظهار (١٠ أحرف على الأقل)
              <input aria-label="سبب إظهار رقم البطاقة" value={revealReason} onChange={(event) => setRevealReason(event.target.value)} placeholder="مثال: التحقق من هوية العميل" />
            </label>
            {revealError && <div className="inline-alert error" role="alert">{revealError}</div>}
            <div className="modal-actions">
              <button className="button button-light" type="button" onClick={() => setShowRevealForm(false)}>إلغاء</button>
              <button className="button button-dark" type="button" disabled={revealBusy || revealReason.trim().length < 10} onClick={() => { void revealNationalId(); }}>{revealBusy ? 'جارٍ الإظهار…' : 'تأكيد الإظهار'}</button>
            </div>
            <small className="muted">يتم تسجيل كل عملية إظهار في سجل التدقيق.</small>
          </div>}
          {revealError && !showRevealForm && <div className="inline-alert error" role="alert">{revealError}</div>}
        </div>
      </section>
      <section className="surface-card customer-profile-section"><h3>التواصل</h3><div className="customer-profile-grid"><div><span>رقم الموبايل</span><strong dir="ltr">{selected.mobile}</strong></div><div><span>البريد الإلكتروني</span><strong dir="ltr">{selected.email || 'غير مسجل'}</strong></div><div><span>واتساب</span><strong>{selected.whatsapp_available ? 'متاح' : 'غير متاح'}</strong></div><div><span>عدد الطلبات</span><strong>{formatNumber(selected.complaints_count)}</strong></div></div></section>
      <section className="surface-card customer-profile-section"><h3>الوثائق والشركات</h3>{selected.policies.length ? selected.policies.map((policy, index) => <article className="customer-profile-item" key={`${policy.policy_number}-${index}`}><strong>{policy.company_name_ar} · {policy.company_name_en}</strong><p>رقم الوثيقة: <bdi>{policy.policy_number}</bdi></p><p>النوع: {policy.insurance_type} · من {policy.effective_from || '—'} إلى {policy.expires_at || '—'}</p></article>) : <div className="empty-inline">لا توجد وثائق مرتبطة.{selected.complaints.length ? ' شركات الطلبات المرتبطة موضحة في سجل الطلبات أدناه.' : ''}</div>}</section>
      <section className="surface-card customer-profile-section"><h3>سجل الطلبات</h3>{selected.complaints.length ? selected.complaints.map((complaint) => <article className="customer-profile-item" key={complaint.ticket_no}><strong>{complaint.ticket_no} · {complaint.subject}</strong><p>{complaint.company_name_ar || complaint.company_name_en} · {complaint.insurance_type}</p><p>الحالة: {complaint.status} · {formatDateTime(complaint.created_at)}</p></article>) : <div className="empty-inline">لا توجد طلبات مسجلة.</div>}</section>
    </div></Modal>}
  </div>;
}
