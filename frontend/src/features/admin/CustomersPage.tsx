import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { hasPermission } from '../../utils/access';
import { formatDateTime, formatNumber } from '../../utils/date';
import type { StaffAccess } from '../../types';

interface Company { id: string; name_en: string; name_ar: string; }
interface Policy { company_name_en: string; company_name_ar: string; insurance_type: string; policy_number: string; effective_from: string | null; expires_at: string | null; }
interface CustomerComplaint { ticket_no: string; subject: string; status: string; created_at: string; company_name_en: string; company_name_ar: string; insurance_type: string; }
interface Customer { id: string; full_name: string; email: string | null; mobile: string; whatsapp_available: boolean; created_at: string; companies: Company[]; policies: Policy[]; complaints: CustomerComplaint[]; complaints_count: number; }

export function CustomersPage({ access }: { access: StaffAccess }) {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Customer | null>(null);
  const [companyFilter, setCompanyFilter] = useState<string>('');
  const [hasPoliciesFilter, setHasPoliciesFilter] = useState<string>('');
  const [hasComplaintsFilter, setHasComplaintsFilter] = useState<string>('');
  const [dateFrom, setDateFrom] = useState<string>('');
  const [dateTo, setDateTo] = useState<string>('');
  useEffect(() => {
    let active = true;
    api<Customer[]>('/api/staff/customers').then((rows) => { if (active) setCustomers(rows); })
      .catch((cause: unknown) => { if (active) setError(cause instanceof Error ? cause.message : 'تعذر تحميل العملاء.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  
  const availableCompanies = useMemo(() => {
    const companiesMap = new Map<string, { id: string; name_ar: string; name_en: string }>();
    customers.forEach((customer) => {
      customer.companies.forEach((company) => {
        if (!companiesMap.has(company.id)) {
          companiesMap.set(company.id, company);
        }
      });
    });
    return Array.from(companiesMap.values()).sort((a, b) => a.name_ar.localeCompare(b.name_ar));
  }, [customers]);
  
  const filtered = useMemo(() => {
    let result = customers;
    const searchNeedle = search.trim().toLocaleLowerCase();
    if (searchNeedle) {
      result = result.filter((customer) => JSON.stringify(customer).toLocaleLowerCase().includes(searchNeedle));
    }
    if (companyFilter) {
      result = result.filter((customer) => 
        customer.companies.some((company) => company.id === companyFilter || company.name_ar.includes(companyFilter) || company.name_en.includes(companyFilter))
      );
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
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { setLoading(true); setError(''); api<Customer[]>('/api/staff/customers').then(setCustomers).catch((e: unknown) => setError(e instanceof Error ? e.message : 'تعذر التحميل.')).finally(() => setLoading(false)); }}>إعادة المحاولة</button></div>}
    <section className="surface-card user-summary-strip"><div className="summary-stat"><span className="summary-stat-icon"><Icon name="users" size={17} /></span><div><strong>{formatNumber(customers.length)}</strong><small>إجمالي العملاء</small></div></div><div className="summary-stat"><span className="summary-stat-icon summary-stat-green"><Icon name="building" size={17} /></span><div><strong>{formatNumber(new Set(customers.flatMap((customer) => customer.companies.map((company) => company.id))).size)}</strong><small>شركات مرتبطة</small></div></div><div className="summary-stat"><span className="summary-stat-icon summary-stat-gold"><Icon name="inbox" size={17} /></span><div><strong>{formatNumber(customers.reduce((count, customer) => count + customer.complaints_count, 0))}</strong><small>إجمالي الشكاوى</small></div></div></section>
    
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
            وجود شكاوى
            <select value={hasComplaintsFilter} onChange={(e) => setHasComplaintsFilter(e.target.value)} style={{ width: '100%' }}>
              <option value="">كل العملاء</option>
              <option value="yes">لديهم شكاوى</option>
              <option value="no">بدون شكاوى</option>
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
    <section className="surface-card admin-table-card"><div className="table-wrap"><table className="data-table admin-users-table"><thead><tr><th>العميل</th><th>وسائل التواصل</th><th>شركة التأمين</th><th>الوثائق</th><th>الشكاوى</th><th>تاريخ التسجيل</th></tr></thead><tbody>
      {loading && <tr><td colSpan={6}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل العملاء…</div></td></tr>}
      {!loading && filtered.map((customer) => {
        const companyNames = customer.companies.length 
          ? customer.companies.map((company) => company.name_ar || company.name_en).join('، ')
          : (customer.policies.length > 0 
              ? customer.policies.map((p) => p.company_name_ar || p.company_name_en).join('، ')
              : 'غير مرتبطة');
        return <tr key={customer.id} onClick={() => setSelected(customer)} style={{ cursor: 'pointer' }} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') setSelected(customer); }}>
          <td><button type="button" className="link-button" onClick={(event) => { event.stopPropagation(); setSelected(customer); }}><strong>{customer.full_name}</strong></button><small className="cell-secondary" dir="ltr">{customer.id}</small></td>
          <td><span dir="ltr">{customer.mobile}</span><small className="cell-secondary" dir="ltr">{customer.email || '—'}</small></td>
          <td>{companyNames}</td>
          <td>{formatNumber(customer.policies.length)}</td>
          <td>{formatNumber(customer.complaints_count)}</td>
          <td>{formatDateTime(customer.created_at)}</td>
        </tr>;
      })}
      {!loading && !filtered.length && <tr><td colSpan={6}><div className="empty-table"><span className="empty-icon"><Icon name="users" size={20} /></span><strong>لا يوجد عملاء مطابقون</strong><span>ابحث بكلمات مختلفة تشمل الاسم أو رقم الوثيقة أو الشركة.</span></div></td></tr>}
    </tbody></table></div></section>
    {selected && <Modal title={selected.full_name} eyebrow="ملف العميل الكامل" onClose={() => setSelected(null)}><div className="page-stack"><section className="surface-card" style={{ padding: 18 }}><h3>بيانات التواصل</h3><p>رقم الموبايل: <bdi>{selected.mobile}</bdi></p><p>البريد الإلكتروني: <bdi>{selected.email || 'غير مسجل'}</bdi></p><p>واتساب: {selected.whatsapp_available ? 'متاح' : 'غير متاح'}</p><p>تاريخ التسجيل: {formatDateTime(selected.created_at)}</p><p>عدد الشكاوى: {formatNumber(selected.complaints_count)}</p></section><section className="surface-card" style={{ padding: 18 }}><h3>شركات التأمين والوثائق</h3>{selected.policies.length ? selected.policies.map((policy, index) => <article key={`${policy.policy_number}-${index}`} style={{ padding: '12px 0', borderTop: '1px solid var(--line, #ddd)' }}><strong>{policy.company_name_ar} · {policy.company_name_en}</strong><p>رقم الوثيقة: <bdi>{policy.policy_number}</bdi></p><p>النوع: {policy.insurance_type} · من {policy.effective_from || '—'} إلى {policy.expires_at || '—'}</p></article>) : <p>لا توجد وثائق مرتبطة.</p>}</section><section className="surface-card" style={{ padding: 18 }}><h3>سجل الشكاوى</h3>{selected.complaints.length ? selected.complaints.map((complaint) => <article key={complaint.ticket_no} style={{ padding: '12px 0', borderTop: '1px solid var(--line, #ddd)' }}><strong>{complaint.ticket_no} · {complaint.subject}</strong><p>{complaint.company_name_ar || complaint.company_name_en} · {complaint.insurance_type}</p><p>الحالة: {complaint.status} · {formatDateTime(complaint.created_at)}</p></article>) : <p>لا توجد شكاوى مسجلة.</p>}</section></div></Modal>}
  </div>;
}
