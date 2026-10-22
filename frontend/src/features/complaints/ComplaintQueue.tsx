import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, apiResponse, jsonBody, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { formatDateTime, formatNumber, rangeBounds } from '../../utils/date';
import type {
  Complaint,
  ComplaintFilterOptions,
  ComplaintListResponse,
  ComplaintPriority,
  ComplaintStatus,
  DateRange,
  InsuranceType,
  SavedView,
  StaffAccess,
} from '../../types';

const STATUS_LABEL: Record<ComplaintStatus, string> = {
  new: 'جديدة',
  assigned: 'مُسندة',
  in_review: 'قيد المراجعة',
  resolved: 'تم الحل',
  reopened: 'أُعيد فتحها',
  transferred: 'محوّلة',
};
const PRIORITY_LABEL: Record<ComplaintPriority, string> = {
  low: 'منخفضة', normal: 'عادية', high: 'مرتفعة', urgent: 'عاجلة',
};
const TYPE_LABEL: Record<InsuranceType, string> = { health: 'طبي', general: 'عام', both: 'مشترك' };
const CHANNEL_LABEL: Record<string, string> = { portal: 'بوابة', email: 'بريد', whatsapp: 'واتساب', phone: 'هاتف', ai_voice: 'صوت ذكي' };
const ALL_COLUMNS = [
  { id: 'ticket_no', label: 'رقم الطلب', locked: true },
  { id: 'subject', label: 'الموضوع' },
  { id: 'company', label: 'شركة التأمين' },
  { id: 'type', label: 'نوع التأمين' },
  { id: 'priority', label: 'الأولوية' },
  { id: 'agent', label: 'الموظف' },
  { id: 'channel', label: 'القناة' },
  { id: 'created_at', label: 'تاريخ الوصول' },
  { id: 'status', label: 'الحالة', locked: true },
] as const;

type SortBy = 'created_at' | 'updated_at' | 'priority' | 'status';
interface QueueFilters {
  status: ComplaintStatus | '';
  company_id: string;
  insurance_type: InsuranceType | '';
  assigned_agent_id: string;
  priority: ComplaintPriority | '';
  channel: string;
  /** Requests arrive by phone and WhatsApp, so the mobile number is how
   *  agents find them when the caller does not know a ticket number. */
  mobile: string;
  card_number: string;
  intake_source: string;
}
const EMPTY_FILTERS: QueueFilters = {
  status: '', company_id: '', insurance_type: '', assigned_agent_id: '', priority: '', channel: '',
  mobile: '', card_number: '', intake_source: '',
};
const INTAKE_SOURCE_LABEL: Record<string, string> = {
  whatsapp: 'واتساب',
  phone: 'مكالمة هاتفية',
  email: 'بريد إلكتروني',
  sms: 'رسالة نصية',
  portal: 'البوابة',
  staff: 'إدخال موظف',
  corporate: 'بوابة الشركة',
};

interface ComplaintQueueProps {
  access: StaffAccess;
  range: DateRange;
  options: ComplaintFilterOptions;
  companyNames: Map<string, string>;
  onOpenTicket: (id: string) => void;
}

function statusClass(status: ComplaintStatus): string {
  return `status-pill status-${status}`;
}

function priorityClass(priority: ComplaintPriority): string {
  return `priority-pill priority-${priority}`;
}

function fileDownload(blob: Blob, filename: string) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
}

export function ComplaintQueue({ access, range, options, companyNames, onOpenTicket }: ComplaintQueueProps) {
  const [moreFilters, setMoreFilters] = useState(false);
  const [filters, setFilters] = useState<QueueFilters>(EMPTY_FILTERS);
  const [search, setSearch] = useState('');

  /** Filters hidden behind the disclosure, so a collapsed panel still says
   *  how much is being filtered out. */
  const extraFilterCount = useMemo(
    () => (['company_id', 'insurance_type', 'assigned_agent_id', 'priority', 'channel', 'card_number'] as const)
      .filter((key) => filters[key] !== '').length,
    [filters],
  );
  const activeFilterCount = useMemo(
    () => Object.values(filters).filter((value) => value !== '').length + (search ? 1 : 0),
    [filters, search],
  );
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [debouncedMobile, setDebouncedMobile] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sortBy, setSortBy] = useState<SortBy>('created_at');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [result, setResult] = useState<ComplaintListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [viewName, setViewName] = useState('');
  const [showSaveView, setShowSaveView] = useState(false);
  const [showColumns, setShowColumns] = useState(false);
  const [visibleColumns, setVisibleColumns] = useState<string[]>(ALL_COLUMNS.map((column) => column.id));
  const [bulkAgent, setBulkAgent] = useState('');
  const [bulkStatus, setBulkStatus] = useState<ComplaintStatus>('in_review');
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 280);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedMobile(filters.mobile.trim()), 320);
    return () => window.clearTimeout(timer);
  }, [filters.mobile]);

  const bounds = useMemo(() => rangeBounds(range), [range]);
  const params = useMemo(() => queryString({
    ...bounds,
    ...filters,
    status: filters.status,
    company_id: filters.company_id,
    insurance_type: filters.insurance_type,
    assigned_agent_id: filters.assigned_agent_id,
    priority: filters.priority,
    channel: filters.channel,
    mobile: debouncedMobile,
    card_number: filters.card_number.trim(),
    intake_source: filters.intake_source,
    search: debouncedSearch,
    page,
    page_size: pageSize,
    sort_by: sortBy,
    sort_dir: sortDir,
  }), [bounds, filters, debouncedMobile, debouncedSearch, page, pageSize, sortBy, sortDir]);

  const load = useCallback(async () => {
    if (!hasPermission(access, 'complaints.view')) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const data = await api<ComplaintListResponse>(`/api/staff/complaints${params}`);
      const items = Array.isArray(data.items) ? data.items : [];
      setResult({ ...data, items });
      setError('');
      setSelected((current) => current.filter((id) => items.some((item) => item.id === id)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل الطلبات.');
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [access, params]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  useEffect(() => {
    if (!hasPermission(access, 'complaints.view')) return;
    void api<SavedView[]>('/api/staff/views').then((views) => setSavedViews(Array.isArray(views) ? views : [])).catch(() => setSavedViews([]));
  }, [access]);

  // Live queue: the staff complaints WebSocket pushes a fresh scoped
  // snapshot whenever tickets change; a quiet poll covers WS outages.
  useEffect(() => {
    if (!hasPermission(access, 'complaints.view')) return;
    let socket: WebSocket | null = null;
    const knownIds = new Set<string>();
    try {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(`${protocol}//${window.location.host}/api/staff/complaints/stream${params}`);
      socket.onmessage = (event: MessageEvent<string>) => {
        try {
          const data = JSON.parse(event.data) as ComplaintListResponse;
          if (Array.isArray(data.items)) {
            const fresh = data.items.some((item) => !knownIds.has(item.id));
            data.items.forEach((item) => knownIds.add(item.id));
            setResult({ ...data, items: data.items });
            setError('');
            setSelected((current) => current.filter((id) => data.items.some((item) => item.id === id)));
            if (fresh) {
              // A ticket nobody had seen before: tell the console (and its
              // notification bell) that staff data changed.
              window.dispatchEvent(new Event('staff:data-changed'));
            }
          }
        } catch { /* Malformed frames are ignored; REST remains the fallback. */ }
      };
    } catch { socket = null; }
    const timer = window.setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send('refresh');
      else if (document.visibilityState === 'visible') void load();
    }, 20_000);
    return () => {
      window.clearInterval(timer);
      socket?.close();
    };
  }, [access, params, load]);

  if (!hasPermission(access, 'complaints.view')) return <AccessDenied title="قائمة الطلبات غير متاحة" />;

  const setFilter = <Key extends keyof QueueFilters>(key: Key, value: QueueFilters[Key]) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setPage(1);
  };

  const changeSort = (column: SortBy) => {
    if (sortBy === column) setSortDir((current) => current === 'asc' ? 'desc' : 'asc');
    else { setSortBy(column); setSortDir('desc'); }
  };

  const applyView = (view: SavedView) => {
    const criteria = view.criteria as Partial<QueueFilters> & { search?: string; sort_by?: SortBy; sort_dir?: 'asc' | 'desc' };
    setFilters({ ...EMPTY_FILTERS, ...criteria });
    setSearch(criteria.search ?? '');
    if (criteria.sort_by) setSortBy(criteria.sort_by);
    if (criteria.sort_dir) setSortDir(criteria.sort_dir);
    setPage(1);
  };

  const createView = async () => {
    if (viewName.trim().length < 2) return;
    try {
      const criteria = {
        ...filters,
        ...bounds,
        search: debouncedSearch || null,
        sort_by: sortBy,
        sort_dir: sortDir,
        page: 1,
        page_size: pageSize,
      };
      const saved = await api<SavedView>('/api/staff/views', {
        method: 'POST', body: jsonBody({ name: viewName.trim(), criteria }),
      });
      setSavedViews((current) => [saved, ...current]);
      setShowSaveView(false);
      setViewName('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حفظ العرض.');
    }
  };

  const runBulk = async (action: 'assign' | 'status') => {
    if (selected.length === 0) return;
    setBulkBusy(true);
    try {
      await api('/api/staff/complaints/bulk', {
        method: 'POST',
        body: jsonBody(action === 'assign'
          ? { action, complaint_ids: selected, assignment: { agent_id: bulkAgent, team_id: null } }
          : { action, complaint_ids: selected, status: bulkStatus }),
      });
      setSelected([]);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تنفيذ الإجراء الجماعي.');
    } finally {
      setBulkBusy(false);
    }
  };

  const exportAs = async () => {
    if (!hasPermission(access, 'complaints.export')) return;
    try {
      const response = await apiResponse(`/api/staff/complaints/export${params.replace(/([?&])page=\d+&?/, '$1').replace(/([?&])page_size=\d+&?/, '$1')}`);
      const csv = await response.text();
      // UTF-8 BOM opens cleanly in Excel without bundling vulnerable SheetJS code.
      fileDownload(new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8' }), `complaints-${range.from}-${range.to}.csv`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تصدير البيانات.');
    }
  };

  const maxPage = Math.max(1, Math.ceil((result?.total ?? 0) / pageSize));
  const selectedAll = Boolean(result?.items.length && result.items.every((item) => selected.includes(item.id)));

  return (
    <div className="page-stack">
      <SectionTitle
        eyebrow="إدارة الحالات"
        title="صندوق الطلبات"
        description="ابحث، صفِّ، وادِر الحالات ضمن نطاق صلاحيتك الحالي."
        action={(
          /* Columns, CSV and print are all incidental: none of them is "the
             action on this screen", so none of them gets the primary fill.
             Printing used to be the loudest control on the page. */
          <div className="queue-heading-actions">
            <button className="button button-light" type="button" onClick={() => setShowColumns((current) => !current)}><Icon name="columns" size={16} /> الأعمدة</button>
            {hasPermission(access, 'complaints.export') && <div className="export-group"><button className="button button-light" type="button" onClick={() => { void exportAs(); }}><Icon name="download" size={15} /> CSV · Excel-ready</button></div>}
            {hasPermission(access, 'complaints.export') && <button className="button button-light" type="button" onClick={() => window.print()}><Icon name="download" size={15} /> PDF / طباعة</button>}
          </div>
        )}
      />

      {showColumns && (
        <div className="columns-popover surface-card">
          <div className="columns-popover-heading"><strong>الأعمدة الظاهرة</strong><button type="button" className="icon-button small" onClick={() => setShowColumns(false)} aria-label="إغلاق"><Icon name="close" size={15} /></button></div>
          {ALL_COLUMNS.map((column) => (
            <label className="check-row" key={column.id}>
              <input
                type="checkbox"
                checked={visibleColumns.includes(column.id)}
                disabled={'locked' in column && column.locked}
                onChange={(event) => setVisibleColumns((current) => event.target.checked ? [...current, column.id] : current.filter((id) => id !== column.id))}
              />
              <span>{column.label}</span>
              {'locked' in column && column.locked && <small>دائماً</small>}
            </label>
          ))}
        </div>
      )}

      {/* Hierarchy over density. Mobile number and intake source are how a
          request is actually found on the phone with a customer waiting, so
          they sit in the primary row with status; the remaining six filters
          are real but rarely needed and fold away behind a count.

          Search and the filters used to be two stacked cards describing one
          job — "narrow this list" — with a hairline, a shadow and 24px of
          gap between them. One card, three rows. */}
      <div className="ds-filters">
        <div className="queue-toolbar">
          <div className="queue-search-wrap"><Icon name="search" size={17} /><input aria-label="البحث في الطلبات" placeholder="ابحث برقم الطلب أو الموضوع…" value={search} onChange={(event) => setSearch(event.target.value)} /><kbd>/</kbd></div>
          <div className="saved-view-control"><Icon name="filter" size={15} /><select aria-label="العروض المحفوظة" value="" onChange={(event) => { const view = savedViews.find((item) => item.id === event.target.value); if (view) applyView(view); }}><option value="">العروض المحفوظة</option>{savedViews.map((view) => <option value={view.id} key={view.id}>{view.name}</option>)}</select></div>
          <button className="button button-quiet save-view-button" type="button" onClick={() => setShowSaveView(true)}><Icon name="plus" size={15} /> حفظ العرض</button>
        </div>

        <div className="ds-filter-primary">
          <label className="ds-field ds-field-grow">
            <span>رقم الموبايل</span>
            <input
              type="tel"
              inputMode="tel"
              dir="ltr"
              placeholder="01xxxxxxxxx"
              value={filters.mobile}
              onChange={(event) => setFilter('mobile', event.target.value)}
            />
          </label>
          <label className="ds-field">
            <span>مصدر الطلب</span>
            <select value={filters.intake_source} onChange={(event) => setFilter('intake_source', event.target.value)}>
              <option value="">كل المصادر</option>
              {Object.entries(INTAKE_SOURCE_LABEL).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
          <label className="ds-field">
            <span>الحالة</span>
            <select value={filters.status} onChange={(event) => setFilter('status', event.target.value as ComplaintStatus | '')}>
              <option value="">كل الحالات</option>
              {options.statuses.map((status) => <option key={status} value={status}>{STATUS_LABEL[status]}</option>)}
            </select>
          </label>
          <button
            className="ds-ghost-btn ds-more-filters"
            type="button"
            aria-expanded={moreFilters}
            onClick={() => setMoreFilters((open) => !open)}
          >
            <Icon name="sliders" size={15} />
            فلاتر إضافية
            {extraFilterCount > 0 && <em className="ds-filter-badge">{formatNumber(extraFilterCount)}</em>}
          </button>
          {activeFilterCount > 0 && (
            <button
              className="ds-quiet-btn"
              type="button"
              onClick={() => { setFilters(EMPTY_FILTERS); setSearch(''); setPage(1); }}
            >
              مسح الفلاتر
            </button>
          )}
        </div>

        {moreFilters && (
          <div className="ds-filter-more">
            <label className="ds-field"><span>شركة التأمين</span><select value={filters.company_id} onChange={(event) => setFilter('company_id', event.target.value)}><option value="">كل شركات نطاقي</option>{options.companies.map((company) => <option value={company.id} key={company.id}>{company.name_ar || company.name_en}</option>)}</select></label>
            <label className="ds-field"><span>نوع التأمين</span><select value={filters.insurance_type} onChange={(event) => setFilter('insurance_type', event.target.value as InsuranceType | '')}><option value="">كل الأنواع</option>{options.insurance_types.map((kind) => <option value={kind} key={kind}>{TYPE_LABEL[kind]}</option>)}</select></label>
            <label className="ds-field"><span>الموظف</span><select value={filters.assigned_agent_id} onChange={(event) => setFilter('assigned_agent_id', event.target.value)}><option value="">كل الموظفين</option>{options.agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.full_name}</option>)}</select></label>
            <label className="ds-field"><span>الأولوية</span><select value={filters.priority} onChange={(event) => setFilter('priority', event.target.value as ComplaintPriority | '')}><option value="">كل الأولويات</option>{options.priorities.map((priority) => <option value={priority} key={priority}>{PRIORITY_LABEL[priority]}</option>)}</select></label>
            <label className="ds-field"><span>القناة</span><select value={filters.channel} onChange={(event) => setFilter('channel', event.target.value)}><option value="">كل القنوات</option>{options.channels.map((channel) => <option value={channel} key={channel}>{CHANNEL_LABEL[channel] ?? channel}</option>)}</select></label>
            <label className="ds-field"><span>رقم الكارت</span><input dir="ltr" placeholder="CARD-0000" value={filters.card_number} onChange={(event) => setFilter('card_number', event.target.value)} /></label>
          </div>
        )}
      </div>

      {selected.length > 0 && (
        <div className="bulk-toolbar">
          <span><strong>{formatNumber(selected.length)}</strong> محددة</span>
          {hasPermission(access, 'complaints.assign') && <div className="bulk-action-group"><select aria-label="إسناد إلى موظف" value={bulkAgent} onChange={(event) => setBulkAgent(event.target.value)}><option value="">اختر الموظف</option>{options.agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.full_name}</option>)}</select><button className="button button-dark button-small" disabled={!bulkAgent || bulkBusy} type="button" onClick={() => { void runBulk('assign'); }}>إسناد</button></div>}
          {hasPermission(access, 'complaints.change_status') && <div className="bulk-action-group"><select aria-label="تغيير الحالة الجماعي" value={bulkStatus} onChange={(event) => setBulkStatus(event.target.value as ComplaintStatus)}>{(['assigned', 'in_review', 'resolved'] as ComplaintStatus[]).map((status) => <option key={status} value={status}>{STATUS_LABEL[status]}</option>)}</select><button className="button button-dark button-small" disabled={bulkBusy} type="button" onClick={() => { void runBulk('status'); }}>تغيير الحالة</button></div>}
          <button className="icon-button small" type="button" aria-label="إلغاء التحديد" onClick={() => setSelected([])}><Icon name="close" size={15} /></button>
        </div>
      )}

      {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}

      <section className="surface-card complaints-table-card">
        <div className="table-topline"><div><strong>{loading && !result ? 'جارٍ التحميل…' : `${formatNumber(result?.total)} طلب`}</strong><span>ضمن الفترة المحددة</span></div><div className="table-range-label"><Icon name="calendar" size={14} />{range.from} — {range.to}</div></div>
        <div className="table-wrap">
          <table className="data-table complaints-table">
            <thead>
              <tr>
                <th className="select-cell"><input aria-label="تحديد كل الطلبات المعروضة" type="checkbox" checked={selectedAll} onChange={(event) => setSelected(event.target.checked ? result?.items.map((item) => item.id) ?? [] : [])} /></th>
                <th>رقم الطلب</th>
                {visibleColumns.includes('subject') && <th>الموضوع</th>}
                {visibleColumns.includes('company') && <th>شركة التأمين</th>}
                {visibleColumns.includes('type') && <th>النوع</th>}
                {visibleColumns.includes('priority') && <th>الأولوية</th>}
                {visibleColumns.includes('agent') && <th>الموظف</th>}
                {visibleColumns.includes('channel') && <th>القناة</th>}
                {visibleColumns.includes('created_at') && <th><button className="sort-button" type="button" onClick={() => changeSort('created_at')}>تاريخ الوصول <span>{sortBy === 'created_at' ? sortDir === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>}
                <th><button className="sort-button" type="button" onClick={() => changeSort('status')}>الحالة <span>{sortBy === 'status' ? sortDir === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>
              </tr>
            </thead>
            <tbody>
              {loading && !result && Array.from({ length: 6 }, (_, index) => <tr className="skeleton-row" key={index}><td colSpan={10}><span /></td></tr>)}
              {!loading && result?.items.map((item: Complaint) => (
                <tr key={item.id} className="complaint-row" onClick={() => onOpenTicket(item.id)} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') onOpenTicket(item.id); }}>
                  <td className="select-cell" onClick={(event) => event.stopPropagation()}><input aria-label={`تحديد ${item.ticket_no}`} type="checkbox" checked={selected.includes(item.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /></td>
                  <td><button className="ticket-number" type="button" onClick={(event) => { event.stopPropagation(); onOpenTicket(item.id); }}>{item.ticket_no}</button><small className="ticket-created">{formatDateTime(item.created_at)}</small></td>
                  {visibleColumns.includes('subject') && <td><strong className="subject-cell">{item.subject}</strong>{item.sla_breached_at && <small className="sla-flag">تجاوز SLA</small>}</td>}
                  {visibleColumns.includes('company') && <td>{companyNames.get(item.company_id) ?? '—'}</td>}
                  {visibleColumns.includes('type') && <td><span className="type-chip">{TYPE_LABEL[item.insurance_type]}</span></td>}
                  {visibleColumns.includes('priority') && <td><span className={priorityClass(item.priority)}>{PRIORITY_LABEL[item.priority]}</span></td>}
                  {visibleColumns.includes('agent') && <td>{options.agents.find((agent) => agent.id === item.assigned_agent_id)?.full_name ?? <span className="muted">غير مُسند</span>}</td>}
                  {visibleColumns.includes('channel') && <td><span className="channel-cell">{CHANNEL_LABEL[item.channel] ?? item.channel}</span></td>}
                  {visibleColumns.includes('created_at') && <td className="date-cell">{formatDateTime(item.created_at)}</td>}
                  <td><span className={statusClass(item.status)}><i />{STATUS_LABEL[item.status]}</span></td>
                </tr>
              ))}
              {!loading && result?.items.length === 0 && <tr><td colSpan={10}><div className="empty-table"><span className="empty-icon"><Icon name="inbox" size={22} /></span><strong>لا توجد طلبات مطابقة</strong><span>جرّب تغيير الفلاتر أو الفترة الزمنية.</span></div></td></tr>}
            </tbody>
          </table>
        </div>
        <div className="table-pagination"><span>عرض {formatNumber(result?.items.length ?? 0)} من {formatNumber(result?.total ?? 0)}</span><div className="pagination-controls"><label>لكل صفحة<select aria-label="عدد العناصر في الصفحة" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option value={10}>١٠</option><option value={20}>٢٠</option><option value={50}>٥٠</option><option value={100}>١٠٠</option></select></label><button className="pagination-arrow" type="button" aria-label="الصفحة السابقة" disabled={page <= 1 || loading} onClick={() => setPage((current) => Math.max(1, current - 1))}><Icon name="chevron" size={15} /></button><strong>{formatNumber(page)} / {formatNumber(maxPage)}</strong><button className="pagination-arrow next" type="button" aria-label="الصفحة التالية" disabled={page >= maxPage || loading} onClick={() => setPage((current) => Math.min(maxPage, current + 1))}><Icon name="chevron" size={15} /></button></div></div>
      </section>

      {showSaveView && <Modal title="حفظ طريقة العرض" eyebrow="تفضيلاتك" onClose={() => setShowSaveView(false)}><p className="modal-intro">احفظ الفلاتر الحالية للعودة إليها لاحقاً. النتائج ستُعاد قراءتها دائماً وفق صلاحياتك الحالية.</p><label className="field-label">اسم العرض<input autoFocus maxLength={80} value={viewName} onChange={(event) => setViewName(event.target.value)} placeholder="مثال: الحالات العاجلة" /></label><div className="modal-actions"><button className="button button-light" type="button" onClick={() => setShowSaveView(false)}>إلغاء</button><button className="button button-dark" type="button" disabled={viewName.trim().length < 2} onClick={() => { void createView(); }}>حفظ العرض</button></div></Modal>}
    </div>
  );
}
