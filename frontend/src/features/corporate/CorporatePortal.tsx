/**
 * The corporate client workspace.
 *
 * Three things the contracted company asked to see in one place: what it is
 * paying and when the contract renews, what its employees have requested
 * (searchable by name), and the documents the broker shared with it.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ApiError } from '../../api';
import { navigate } from '../../lib/router';
import { Icon } from '../../components/Icon';
import { usePlatformBrand } from '../../components/Branding';
import { Blank, Metric, Panel, Stagger, StaggerItem, SwapPanel } from '../../components/ui/Premium';
import { formatNumber } from '../../utils/date';
import { RequestArchive } from './RequestArchive';
import {
  CONTRACT_STATUS_LABEL,
  INTAKE_SOURCE_LABEL,
  PAYMENT_FREQUENCY_LABEL,
  REQUEST_STATUS_LABEL,
  corporateApi,
  documentKindLabel,
  formatBytes,
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

/* `fileSize` used to live here, a second implementation of `formatBytes`
   that printed Latin digits — so the contract tab and the archive tab sized
   the same PDF two different ways. */

function DocumentRow({ document }: { document: CorporateDocument }) {
  return (
    <li className="corporate-doc">
      <div className="corporate-doc-main">
        <span className="corporate-doc-title">{document.title || document.file_name || 'مستند'}</span>
        <span className="corporate-doc-meta">
          {documentKindLabel(document.kind)} · {formatBytes(document.byte_size)}
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
  const activeContract = contracts.find((contract) => contract.status === 'active') ?? contracts[0];
  return (
    <div className="corporate-overview">
      <section className="corporate-welcome-banner">
        <div>
          <span>أهلاً بك</span>
          <strong className="corporate-welcome-company">{data.client.name_ar || data.client.name_en}</strong>
          <p>{activeContract ? `عقد ${activeContract.contract_no} · يتجدد ${formatDate(activeContract.renews_on)}` : 'بوابة شركتك لدى وسيط'}</p>
        </div>
        <div className="corporate-covered-count"><strong>{formatNumber(data.employees_count)}</strong><span>موظف مغطّى</span></div>
      </section>
      {/* The same four-up KPI row the staff dashboard uses, from the same
          primitive, so the two halves of the product count things the same
          way — including the digits: this grid printed 430 and 42 in Latin
          numerals beside an Arabic-Indic contract value. */}
      <Stagger className="ds-kpi-grid">
        <StaggerItem>
          <Metric
            label="الطلبات المفتوحة"
            value={formatNumber(data.requests_open)}
            hint="قيد المعالجة الآن"
            icon={<Icon name="inbox" size={17} />}
            tone="gold"
          />
        </StaggerItem>
        <StaggerItem>
          <Metric
            label="إجمالي الطلبات"
            value={formatNumber(data.requests_total)}
            hint="منذ بداية التعاقد"
            icon={<Icon name="message" size={17} />}
            tone="info"
          />
        </StaggerItem>
        <StaggerItem>
          <Metric
            label="عدد الموظفين"
            value={formatNumber(data.employees_count)}
            hint="المشمولون بالتغطية"
            icon={<Icon name="users" size={17} />}
            tone="success"
          />
        </StaggerItem>
        <StaggerItem>
          <Metric
            label="عدد التعاقدات"
            value={formatNumber(contracts.length)}
            hint="السارية والمنتهية"
            icon={<Icon name="shield" size={17} />}
            tone="violet"
          />
        </StaggerItem>
      </Stagger>

      <section className="corporate-section">
        <h2>التعاقد مع الوسيط</h2>
        {contracts.length === 0 ? (
          <Panel>
            <Blank
              icon={<Icon name="shield" size={22} />}
              title="لا توجد تعاقدات مسجلة بعد"
              body="بمجرد توقيع التعاقد مع الوسيط ستظهر قيمته وتاريخ تجديده والمستندات المرفقة هنا."
            />
          </Panel>
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
                      <dd>{contract.headcount == null ? '—' : formatNumber(contract.headcount)}</dd>
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
          {formatNumber(member.requests_count)} طلب
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
      {loading && (
        <Panel aria-busy="true" aria-label="جارٍ تحميل قائمة الموظفين">
          <div className="ds-loading-lines"><span /><span /><span /><span /></div>
        </Panel>
      )}
      {!loading && !error && members.length === 0 && (
        <Panel>
          <Blank
            icon={<Icon name="users" size={22} />}
            title={debounced ? 'لا يوجد موظف مطابق' : 'لا يوجد موظفون مسجلون بعد'}
            body={debounced
              ? `لم نعثر على نتيجة لـ «${debounced}». جرّب الاسم الأول أو كود الموظف أو آخر أربعة أرقام من الموبايل.`
              : 'سيظهر هنا كل موظف مشمول بالتغطية، ومعه الطلبات التي قدّمها.'}
          />
        </Panel>
      )}
      {!loading && members.length > 0 && (
        <>
          <p className="corporate-result-count">{formatNumber(total)} موظف</p>
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
          <Panel>
            <Blank
              icon={<Icon name="paperclip" size={22} />}
              title="لا توجد وثائق مسجلة بعد"
              body="وثائق التأمين السارية لشركتك ستظهر هنا بأرقامها وتواريخ سريانها."
            />
          </Panel>
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
        {loading && (
          <Panel aria-busy="true" aria-label="جارٍ تحميل المستندات">
            <div className="ds-loading-lines"><span /><span /><span /></div>
          </Panel>
        )}
        {!loading && !error && all.length === 0 && (
          <Panel>
            <Blank
              icon={<Icon name="paperclip" size={22} />}
              title="لم تتم مشاركة مستندات معك بعد"
              body="كل مستند يرفعه فريق الوساطة لشركتك سيصبح متاحاً للتنزيل من هنا."
            />
          </Panel>
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

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: 'التعاقد' },
  { id: 'members', label: 'طلبات الموظفين' },
  { id: 'archive', label: 'أرشيف الطلبات' },
  { id: 'documents', label: 'الوثائق والمستندات' },
];

export function CorporatePortal() {
  const [tab, setTab] = useState<Tab>('overview');
  const [overview, setOverview] = useState<CorporateOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const brand = usePlatformBrand();
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'));

  const toggleTheme = () => {
    const next = !document.documentElement.classList.contains('dark');
    document.documentElement.classList.toggle('dark', next);
    try {
      localStorage.setItem('insurance-portal:theme', next ? 'dark' : 'light');
    } catch {
      // Storage may be blocked; the class still applies for this session.
    }
    setDark(next);
  };

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

  /* The portal opened straight onto a bare <h1> with no chrome: no mark, no
     way to change theme, no sense of which product you were in. It gets the
     same top bar the customer portal and the console have. */
  const chrome = (children: ReactNode) => (
    <div className="corporate-shell">
      <header className="corporate-topbar">
        <div className="corporate-brand">
          <img src={brand.logo_url} width={34} height={34} alt="" aria-hidden="true" />
          <div>
            <strong>{brand.platform_name}</strong>
            <small>بوابة الشركات المتعاقدة</small>
          </div>
        </div>
        <div className="corporate-topbar-actions">
          <button
            type="button"
            className="corporate-icon-button"
            onClick={toggleTheme}
            aria-label={dark ? 'تفعيل الوضع الفاتح' : 'تفعيل الوضع الداكن'}
            title="تبديل المظهر"
          >
            <Icon name={dark ? 'sun' : 'moon'} size={17} />
          </button>
          <a className="button button-light" href="/corporate/login">تبديل الحساب</a>
        </div>
      </header>
      {children}
    </div>
  );

  if (loading) {
    return chrome(
      <div className="corporate-body" aria-busy="true">
        <div className="corporate-header">
          <div>
            <span className="corporate-eyebrow">بوابة الشركات</span>
            <h1 className="ds-skeleton corporate-title-skeleton" aria-hidden="true">&nbsp;</h1>
          </div>
        </div>
        <div className="ds-kpi-grid">
          {[0, 1, 2, 3].map((index) => (
            <Panel key={index} className="ds-metric-card">
              <div className="ds-loading-lines"><span /><span /></div>
            </Panel>
          ))}
        </div>
        <Panel>
          <div className="ds-loading-lines"><span /><span /><span /><span /></div>
        </Panel>
      </div>,
    );
  }

  if (error) {
    return chrome(
      <div className="corporate-body">
        <Panel>
          <Blank
            icon={<Icon name="alert" size={22} />}
            title="تعذر تحميل بيانات الشركة"
            body={error}
            action={<button className="button button-dark" type="button" onClick={() => void load()}>إعادة المحاولة</button>}
          />
        </Panel>
      </div>,
    );
  }

  if (!overview) return null;

  return chrome(
    <div className="corporate-body">
      <header className="corporate-header">
        <div>
          <span className="corporate-eyebrow">بوابة الشركات</span>
          <h1>{overview.client.name_ar || overview.client.name_en}</h1>
          <p className="corporate-header-sub">
            التعاقد، طلبات الموظفين، والمستندات المشتركة مع فريق الوساطة — في مكان واحد.
          </p>
        </div>
      </header>

      <nav className="corporate-tabs ds-segment" aria-label="أقسام البوابة">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={tab === item.id ? 'is-active' : ''}
            aria-current={tab === item.id ? 'page' : undefined}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <SwapPanel swapKey={tab}>
        {tab === 'overview' && <OverviewTab data={overview} />}
        {tab === 'members' && <MembersTab />}
        {tab === 'archive' && <RequestArchive />}
        {tab === 'documents' && <DocumentsTab overview={overview} />}
      </SwapPanel>
    </div>,
  );
}
