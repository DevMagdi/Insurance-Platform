/**
 * The corporate client workspace.
 *
 * Three things the contracted company asked to see in one place: what it is
 * paying and when the contract renews, what its employees have requested
 * (searchable by name), and the documents the broker shared with it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError } from '../../api';
import { navigate } from '../../lib/router';
import { RequestArchive } from './RequestArchive';
import {
  CONTRACT_STATUS_LABEL,
  INTAKE_SOURCE_LABEL,
  PAYMENT_FREQUENCY_LABEL,
  REQUEST_STATUS_LABEL,
  corporateApi,
  formatDate,
  formatMoney,
  renewalNotice,
  type CorporateDocument,
  type CorporateMember,
  type CorporateOverview,
} from '../../lib/corporate';

type Tab = 'overview' | 'members' | 'archive' | 'documents';

function statusLabel(status: string): string {
  return REQUEST_STATUS_LABEL[status] ?? status;
}

function sourceLabel(source: string | null): string {
  if (!source) return '—';
  return INTAKE_SOURCE_LABEL[source] ?? source;
}

function fileSize(bytes: number | null): string {
  if (bytes === null || bytes <= 0) return '—';
  if (bytes < 1024) return `${bytes} بايت`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} ك.ب`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} م.ب`;
}

function DocumentRow({ document }: { document: CorporateDocument }) {
  return (
    <li className="corporate-doc">
      <div className="corporate-doc-main">
        <span className="corporate-doc-title">{document.title || document.file_name || 'مستند'}</span>
        <span className="corporate-doc-meta">
          {document.kind ?? 'مستند'} · {fileSize(document.byte_size)}
          {document.uploaded_at ? ` · ${formatDate(document.uploaded_at)}` : ''}
        </span>
      </div>
      <a
        className="button button-light"
        href={corporateApi.documentUrl(document.id)}
        download={document.file_name ?? undefined}
      >
        تحميل
      </a>
    </li>
  );
}

function OverviewTab({ data }: { data: CorporateOverview }) {
  const contracts = data.contracts ?? [];
  return (
    <div className="corporate-overview">
      <div className="corporate-stat-grid">
        <div className="corporate-stat surface-card">
          <span className="corporate-stat-label">الطلبات المفتوحة</span>
          <strong className="corporate-stat-value">{data.requests_open}</strong>
        </div>
        <div className="corporate-stat surface-card">
          <span className="corporate-stat-label">إجمالي الطلبات</span>
          <strong className="corporate-stat-value">{data.requests_total}</strong>
        </div>
        <div className="corporate-stat surface-card">
          <span className="corporate-stat-label">عدد الموظفين</span>
          <strong className="corporate-stat-value">{data.employees_count}</strong>
        </div>
        <div className="corporate-stat surface-card">
          <span className="corporate-stat-label">عدد التعاقدات</span>
          <strong className="corporate-stat-value">{contracts.length}</strong>
        </div>
      </div>

      <section className="corporate-section">
        <h2>التعاقد مع الوسيط</h2>
        {contracts.length === 0 ? (
          <p className="corporate-empty">لا توجد تعاقدات مسجلة بعد.</p>
        ) : (
          <div className="corporate-contract-grid">
            {contracts.map((contract) => {
              const renewal = renewalNotice(contract.days_to_renewal);
              return (
                <article className="corporate-contract surface-card" key={contract.id}>
                  <header className="corporate-contract-head">
                    <span className="corporate-contract-no" dir="ltr">{contract.contract_no}</span>
                    <span className={`corporate-chip corporate-chip-${contract.status}`}>
                      {CONTRACT_STATUS_LABEL[contract.status] ?? contract.status}
                    </span>
                  </header>
                  <dl className="corporate-contract-facts">
                    <div>
                      <dt>قيمة التعاقد</dt>
                      <dd className="corporate-amount">
                        {formatMoney(contract.contract_value, contract.currency)}
                      </dd>
                    </div>
                    <div>
                      <dt>نوع الدفع</dt>
                      <dd>{PAYMENT_FREQUENCY_LABEL[contract.payment_frequency] ?? contract.payment_frequency}</dd>
                    </div>
                    <div>
                      <dt>تاريخ التجديد</dt>
                      <dd>{formatDate(contract.renews_on)}</dd>
                    </div>
                    <div>
                      <dt>حالة التجديد</dt>
                      <dd className={`corporate-renewal corporate-renewal-${renewal.tone}`}>{renewal.text}</dd>
                    </div>
                    <div>
                      <dt>بداية التعاقد</dt>
                      <dd>{formatDate(contract.starts_on)}</dd>
                    </div>
                    <div>
                      <dt>عدد المشمولين</dt>
                      <dd>{contract.headcount ?? '—'}</dd>
                    </div>
                  </dl>
                  {contract.documents.length > 0 && (
                    <ul className="corporate-doc-list">
                      {contract.documents.map((document) => (
                        <DocumentRow document={document} key={document.id} />
                      ))}
                    </ul>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="corporate-section">
        <h2>بيانات الشركة</h2>
        <dl className="corporate-client-facts surface-card">
          <div><dt>الاسم</dt><dd>{data.client.name_ar || data.client.name_en}</dd></div>
          <div><dt>الاسم بالإنجليزية</dt><dd dir="ltr">{data.client.name_en || '—'}</dd></div>
          <div><dt>مسؤول التواصل</dt><dd>{data.client.contact_name ?? '—'}</dd></div>
          <div><dt>البريد الإلكتروني</dt><dd dir="ltr">{data.client.contact_email ?? '—'}</dd></div>
          <div><dt>رقم الموبايل</dt><dd dir="ltr">{data.client.contact_mobile ?? '—'}</dd></div>
          <div><dt>العنوان</dt><dd>{data.client.address ?? '—'}</dd></div>
        </dl>
      </section>
    </div>
  );
}

function MemberCard({ member }: { member: CorporateMember }) {
  const [open, setOpen] = useState(false);
  const hasRequests = member.requests.length > 0;
  return (
    <article className="corporate-member surface-card">
      <header className="corporate-member-head">
        <div>
          <h3>{member.full_name}</h3>
          <span className="corporate-member-meta">
            {member.employee_code ? <span dir="ltr">{member.employee_code}</span> : null}
            {member.employee_code && member.mobile ? ' · ' : ''}
            {member.mobile ? <span dir="ltr">{member.mobile}</span> : null}
          </span>
        </div>
        <button
          className="corporate-count"
          type="button"
          disabled={!hasRequests}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {member.requests_count} طلب
        </button>
      </header>
      {open && hasRequests && (
        <ul className="corporate-request-list">
          {member.requests.map((item) => (
            <li className="corporate-request" key={item.id}>
              <span className="corporate-request-ticket" dir="ltr">{item.ticket_no}</span>
              <span className="corporate-request-subject">{item.subject || 'بدون موضوع'}</span>
              <span className={`corporate-chip corporate-chip-${item.status}`}>{statusLabel(item.status)}</span>
              <span className="corporate-request-source">{sourceLabel(item.intake_source)}</span>
              <span className="corporate-request-ref" dir="ltr">
                {item.policy_number || item.card_number || '—'}
              </span>
              <span className="corporate-request-date">{formatDate(item.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function MembersTab() {
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [members, setMembers] = useState<CorporateMember[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    let active = true;
    const run = () => {
      if (!active) return;
      setLoading(true);
      corporateApi
        .members(debounced)
        .then((data) => {
          if (!active) return;
          setMembers(data.items ?? []);
          setTotal(data.total ?? 0);
          setError('');
        })
        .catch((caught: unknown) => {
          if (!active) return;
          setError(caught instanceof ApiError ? caught.message : 'تعذر تحميل قائمة الموظفين.');
          setMembers([]);
        })
        .finally(() => { if (active) setLoading(false); });
    };
    queueMicrotask(run);
    return () => { active = false; };
  }, [debounced]);

  return (
    <div className="corporate-members">
      <div className="corporate-search surface-card">
        <input
          type="search"
          aria-label="بحث باسم الموظف"
          placeholder="ابحث باسم الموظف أو كوده أو رقم موبايله…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {search && (
          <button className="button button-light" type="button" onClick={() => setSearch('')}>
            مسح
          </button>
        )}
      </div>

      {error && <div className="corporate-error" role="alert">{error}</div>}
      {loading && <p className="corporate-empty">جارٍ التحميل…</p>}
      {!loading && !error && members.length === 0 && (
        <p className="corporate-empty">
          {debounced ? `لا يوجد موظف مطابق لـ «${debounced}».` : 'لا يوجد موظفون مسجلون بعد.'}
        </p>
      )}
      {!loading && members.length > 0 && (
        <>
          <p className="corporate-result-count">{total} موظف</p>
          <div className="corporate-member-list">
            {members.map((member) => <MemberCard member={member} key={member.id} />)}
          </div>
        </>
      )}
    </div>
  );
}

function DocumentsTab({ overview }: { overview: CorporateOverview | null }) {
  const [documents, setDocuments] = useState<CorporateDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      corporateApi
        .documents()
        .then((data) => { if (active) { setDocuments(data.items ?? []); setError(''); } })
        .catch((caught: unknown) => {
          if (active) setError(caught instanceof ApiError ? caught.message : 'تعذر تحميل المستندات.');
        })
        .finally(() => { if (active) setLoading(false); });
    });
    return () => { active = false; };
  }, []);

  // Contract attachments live on the contract itself; show them here too so
  // "the documents" is one list rather than a hunt across tabs.
  const contractDocs = useMemo(
    () => (overview?.contracts ?? []).flatMap((contract) => contract.documents),
    [overview],
  );
  const all = useMemo(() => {
    const seen = new Set<string>();
    return [...documents, ...contractDocs].filter((document) => {
      if (seen.has(document.id)) return false;
      seen.add(document.id);
      return true;
    });
  }, [documents, contractDocs]);

  const policies = overview?.policies ?? [];

  return (
    <div className="corporate-documents">
      <section className="corporate-section">
        <h2>وثائق التأمين</h2>
        {policies.length === 0 ? (
          <p className="corporate-empty">لا توجد وثائق مسجلة بعد.</p>
        ) : (
          <ul className="corporate-policy-list">
            {policies.map((policy) => (
              <li className="corporate-policy surface-card" key={policy.id}>
                <span className="corporate-policy-no" dir="ltr">{policy.policy_number ?? '—'}</span>
                <span>{policy.insurance_type ?? '—'}</span>
                <span>{policy.company_name ?? '—'}</span>
                <span className="corporate-policy-dates">
                  {formatDate(policy.starts_on)} — {formatDate(policy.ends_on)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="corporate-section">
        <h2>المستندات</h2>
        {error && <div className="corporate-error" role="alert">{error}</div>}
        {loading && <p className="corporate-empty">جارٍ التحميل…</p>}
        {!loading && !error && all.length === 0 && (
          <p className="corporate-empty">لم تتم مشاركة مستندات معك بعد.</p>
        )}
        {all.length > 0 && (
          <ul className="corporate-doc-list">
            {all.map((document) => <DocumentRow document={document} key={document.id} />)}
          </ul>
        )}
      </section>
    </div>
  );
}

export function CorporatePortal() {
  const [tab, setTab] = useState<Tab>('overview');
  const [overview, setOverview] = useState<CorporateOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setOverview(await corporateApi.overview());
      setError('');
    } catch (caught) {
      // An unauthenticated visitor belongs on the sign-in page, not on an
      // error screen they cannot act on.
      if (caught instanceof ApiError && caught.status === 401) {
        navigate('/corporate/login');
        return;
      }
      setError(caught instanceof ApiError ? caught.message : 'تعذر تحميل بيانات الشركة.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  if (loading) return <div className="corporate-shell"><p className="corporate-empty">جارٍ التحميل…</p></div>;
  if (error) {
    return (
      <div className="corporate-shell">
        <div className="corporate-error" role="alert">{error}</div>
        <button className="button button-dark" type="button" onClick={() => void load()}>إعادة المحاولة</button>
      </div>
    );
  }
  if (!overview) return null;

  return (
    <div className="corporate-shell">
      <header className="corporate-header">
        <div>
          <span className="corporate-eyebrow">بوابة الشركات</span>
          <h1>{overview.client.name_ar || overview.client.name_en}</h1>
        </div>
        <a className="button button-light" href="/corporate/login">تبديل الحساب</a>
      </header>

      <nav className="corporate-tabs" aria-label="أقسام البوابة">
        <button type="button" className={tab === 'overview' ? 'is-active' : ''} onClick={() => setTab('overview')}>
          التعاقد
        </button>
        <button type="button" className={tab === 'members' ? 'is-active' : ''} onClick={() => setTab('members')}>
          طلبات الموظفين
        </button>
        <button type="button" className={tab === 'archive' ? 'is-active' : ''} onClick={() => setTab('archive')}>
          أرشيف الطلبات
        </button>
        <button type="button" className={tab === 'documents' ? 'is-active' : ''} onClick={() => setTab('documents')}>
          الوثائق والمستندات
        </button>
      </nav>

      {tab === 'overview' && <OverviewTab data={overview} />}
      {tab === 'members' && <MembersTab />}
      {tab === 'archive' && <RequestArchive />}
      {tab === 'documents' && <DocumentsTab overview={overview} />}
    </div>
  );
}
