import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import { Icon } from '../../components/Icon';
import type { StaffAccess } from '../../types';
import { hasPermission } from '../../utils/access';
import './sales-clients.css';

interface SalesClient {
  id: string;
  name_ar?: string | null;
  name_en?: string | null;
  slug?: string | null;
  contact_name?: string | null;
  contact_mobile?: string | null;
  contact_email?: string | null;
  converted_at?: string | null;
  lead_id?: string | null;
  lead_no?: string | null;
  origin?: string;
}
interface DirectoryResponse { items: SalesClient[]; total?: number; scope?: string }
interface ConversionNotice { id: string; name: string; at: number }
const NOTICE_KEY = 'brokerhub:last-converted-client';

export function SalesClientsPage({ access, onOpenFolder }: {
  access: StaffAccess;
  onOpenFolder: (clientId: string) => void;
}) {
  const [clients, setClients] = useState<SalesClient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [dismissedBannerId, setDismissedBannerId] = useState<string | null>(null);
  const [mountedAt] = useState(() => Date.now());
  const [notice, setNotice] = useState<ConversionNotice | null>(() => {
    try {
      const value = window.localStorage.getItem(NOTICE_KEY);
      return value ? JSON.parse(value) as ConversionNotice : null;
    } catch { return null; }
  });
  const canViewCrm = hasPermission(access, 'crm.view');
  const canViewDirectory = canViewCrm || hasPermission(access, 'corporate.view');

  useEffect(() => {
    if (!canViewDirectory) return;
    let active = true;
    api<DirectoryResponse>('/api/staff/corporate/sales-directory')
      .then((result) => { if (active) setClients(result.items ?? []); })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof Error ? cause.message : 'تعذر تحميل قائمة العملاء.');
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [canViewDirectory]);

  const visibleClients = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return clients;
    return clients.filter((client) => [client.name_ar, client.name_en, client.contact_name, client.contact_mobile, client.contact_email, client.lead_no]
      .some((value) => value?.toLocaleLowerCase().includes(query)));
  }, [clients, search]);
  const noticeClient = notice ? clients.find((client) => client.id === notice.id) : undefined;
  const latestConvertedClient = [...clients]
    .filter((client) => client.converted_at)
    .sort((a, b) => Date.parse(b.converted_at ?? '') - Date.parse(a.converted_at ?? ''))[0];
  const recentServerConversion = latestConvertedClient?.converted_at
    && mountedAt - Date.parse(latestConvertedClient.converted_at) < 7 * 24 * 60 * 60 * 1000
    ? latestConvertedClient
    : undefined;
  const convertedClient = (noticeClient ?? recentServerConversion)?.id === dismissedBannerId
    ? undefined
    : noticeClient ?? recentServerConversion;

  if (!canViewDirectory) return <section className="surface-card"><p role="status">قائمة العملاء غير متاحة لصلاحيات حسابك.</p></section>;

  return <div className="page-stack sales-clients-page" dir="rtl">
    <header className="sales-clients-heading">
      <div><span className="sales-clients-eyebrow">إدارة العلاقات</span><h1>العملاء</h1><p>عملاؤك الناتجون عن تحويلات الـ Leads المسندة إليك.</p></div>
      <div className="sales-clients-count"><strong>{clients.length}</strong><span>عميل</span></div>
    </header>

    {convertedClient && <div className="sales-client-converted" role="status">
      <span className="sales-client-converted-icon"><Icon name="check" size={18} /></span>
      <div><strong>تم التحويل بنجاح</strong><p>«{convertedClient.name_ar ?? convertedClient.name_en ?? notice?.name}» أصبح في قائمة عملائك ويمكن فتح ملفه الآن.</p></div>
      <button type="button" className="button button-light" onClick={() => onOpenFolder(convertedClient.id)}>فتح ملف العميل</button>
      <button type="button" className="sales-client-dismiss" aria-label="إغلاق إشعار التحويل" onClick={() => { setDismissedBannerId(convertedClient.id); setNotice(null); window.localStorage.removeItem(NOTICE_KEY); }}>×</button>
    </div>}

    {error && <div className="inline-alert error" role="alert">{error}</div>}
    <section className="surface-card sales-clients-card">
      <div className="sales-clients-toolbar">
        <div><h2>قائمة العملاء المرتبطين بك</h2><p>{clients.length} عميل من الـ Leads المحوّلة</p></div>
        <label className="sales-clients-search"><Icon name="search" size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث بالاسم أو الهاتف أو رقم الـ Lead" aria-label="البحث في العملاء" /></label>
      </div>
      {loading ? <div className="empty-inline" role="status">جارٍ تحميل العملاء…</div> : visibleClients.length === 0
        ? <div className="sales-clients-empty"><span><Icon name="building" size={22} /></span><strong>{search ? 'لا توجد نتائج مطابقة' : 'لا يوجد عملاء محوّلون بعد'}</strong><p>{search ? 'جرّب البحث باسم العميل أو رقم الهاتف.' : 'عند تحويل Lead مسند إليك، سيظهر العميل هنا مباشرة.'}</p></div>
        : <div className="sales-clients-table-wrap"><table className="data-table sales-clients-table"><thead><tr><th>العميل</th><th>جهة التواصل</th><th>الهاتف</th><th>تاريخ التحويل</th><th>المصدر</th><th /></tr></thead><tbody>
          {visibleClients.map((client) => <tr key={client.id}>
            <td><strong>{client.name_ar ?? client.name_en ?? client.slug ?? 'عميل'}</strong>{client.lead_no && <small>{client.lead_no}</small>}</td>
            <td>{client.contact_name ?? '—'}</td><td dir="ltr">{client.contact_mobile ?? '—'}</td>
            <td>{client.converted_at ? new Intl.DateTimeFormat('ar-EG', { dateStyle: 'medium' }).format(new Date(client.converted_at)) : '—'}</td>
            <td><span className="sales-client-source">Lead محوّل</span></td>
            <td><button type="button" className="button button-light button-small" onClick={() => onOpenFolder(client.id)}>فتح الملف</button></td>
          </tr>)}
        </tbody></table></div>}
    </section>
  </div>;
}
