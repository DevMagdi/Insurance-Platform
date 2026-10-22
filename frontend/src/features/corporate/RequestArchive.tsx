/**
 * The company's request archive.
 *
 * Everything its employees ever raised, in one searchable list that keeps
 * working after a request is closed. The filters compose, the counters
 * describe the filtered set rather than the page, and any row opens into the
 * full record: messages, attachments, phone calls and status history.
 *
 * Deliberately not here: message bodies the brokerage marked internal, and
 * call recordings. The archive shows that a nine-minute call happened on the
 * 3rd; listening to it is a separate, separately audited action.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError } from '../../api';
import { Icon } from '../../components/Icon';
import { Blank } from '../../components/ui/Premium';
import {
  EVENT_TYPE_LABEL,
  INTAKE_SOURCE_LABEL,
  REQUEST_STATUS_LABEL,
  corporateApi,
  formatBytes,
  formatDate,
  formatDuration,
  type ArchivePage,
  type ArchiveRequest,
  type RequestTimeline,
} from '../../lib/corporate';

const PAGE_SIZE = 25;

const STATUS_OPTIONS = ['new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'];
const SOURCE_OPTIONS = ['whatsapp', 'phone', 'email', 'sms', 'portal', 'staff', 'corporate'];

function statusLabel(status: string): string {
  return REQUEST_STATUS_LABEL[status] ?? status;
}

function sourceLabel(source: string | null): string {
  if (!source) return '—';
  return INTAKE_SOURCE_LABEL[source] ?? source;
}

function eventLabel(eventType: string): string {
  return EVENT_TYPE_LABEL[eventType] ?? eventType;
}

/** The reference a request was opened against: policy, card, or neither yet. */
function referenceLabel(request: Pick<ArchiveRequest, 'policy_number' | 'card_number'>): string {
  if (request.policy_number) return `وثيقة ${request.policy_number}`;
  if (request.card_number && request.card_number !== 'PENDING-REF') {
    return `كارنيه ${request.card_number}`;
  }
  return 'بانتظار رقم الوثيقة أو الكارنيه';
}

function TimelinePanel({ requestId, onClose }: { requestId: string; onClose: () => void }) {
  const [timeline, setTimeline] = useState<RequestTimeline | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    // Every state write is deferred: setting state synchronously inside an
    // effect body is a lint error here, and it also makes the first paint
    // flicker.
    queueMicrotask(() => {
      if (!active) return;
      setLoading(true);
      void corporateApi
        .requestTimeline(requestId)
        .then((data) => {
          if (active) {
            setTimeline(data);
            setError('');
          }
        })
        .catch((caught: unknown) => {
          if (active) setError(caught instanceof ApiError ? caught.message : 'تعذر فتح الطلب.');
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    });
    return () => {
      active = false;
    };
  }, [requestId]);

  return (
    <section className="archive-detail surface-card" aria-label="تفاصيل الطلب">
      <header className="archive-detail-head">
        <h3>{timeline ? timeline.ticket_no : 'تفاصيل الطلب'}</h3>
        <button type="button" className="button button-light" onClick={onClose}>
          إغلاق
        </button>
      </header>

      {loading && (
        <div className="ds-loading-lines" aria-busy="true" aria-label="جارٍ تحميل تفاصيل الطلب">
          <span /><span /><span />
        </div>
      )}
      {error && (
        <div className="corporate-error" role="alert">
          {error}
        </div>
      )}

      {timeline && !loading && (
        <div className="archive-detail-body">
          <dl className="archive-facts">
            <div>
              <dt>الموظف</dt>
              <dd>{timeline.employee?.full_name ?? '—'}</dd>
            </div>
            <div>
              <dt>رقم الموبايل</dt>
              <dd dir="ltr">{timeline.employee?.mobile ?? '—'}</dd>
            </div>
            <div>
              <dt>المرجع</dt>
              <dd>{referenceLabel(timeline)}</dd>
            </div>
            <div>
              <dt>مصدر الطلب</dt>
              <dd>{sourceLabel(timeline.intake_source)}</dd>
            </div>
            <div>
              <dt>الحالة</dt>
              <dd>{statusLabel(timeline.status)}</dd>
            </div>
            <div>
              <dt>تاريخ الإنشاء</dt>
              <dd>{formatDate(timeline.created_at)}</dd>
            </div>
          </dl>

          <section className="archive-group">
            <h4>المراسلات ({timeline.messages.length})</h4>
            {timeline.messages.length === 0 ? (
              <p className="archive-none">لا توجد مراسلات ظاهرة.</p>
            ) : (
              <ul className="archive-messages">
                {timeline.messages.map((message) => (
                  <li key={message.id} className={`archive-message is-${message.direction}`}>
                    <span className="archive-message-meta">
                      {message.direction === 'inbound' ? 'من الموظف' : 'من الوسيط'} ·{' '}
                      {sourceLabel(message.channel)} · {formatDate(message.created_at)}
                    </span>
                    <p className="archive-message-body">{message.body || '—'}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="archive-group">
            <h4>المرفقات ({timeline.attachments.length})</h4>
            {timeline.attachments.length === 0 ? (
              <p className="archive-none">لا توجد مرفقات.</p>
            ) : (
              <ul className="archive-attachments">
                {timeline.attachments.map((attachment) => (
                  <li key={attachment.id}>
                    <span>{attachment.filename ?? 'مرفق'}</span>
                    <span className="archive-muted">
                      {formatBytes(attachment.size_bytes)} · {formatDate(attachment.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="archive-group">
            <h4>المكالمات ({timeline.calls.length})</h4>
            {timeline.calls.length === 0 ? (
              <p className="archive-none">لا توجد مكالمات.</p>
            ) : (
              <ul className="archive-calls">
                {timeline.calls.map((call) => (
                  <li key={call.id}>
                    <span>
                      {call.direction === 'inbound' ? 'مكالمة واردة' : 'مكالمة صادرة'} ·{' '}
                      {formatDuration(call.duration_seconds)}
                    </span>
                    <span className="archive-muted">
                      {formatDate(call.started_at)}
                      {call.recorded ? ' · مسجّلة' : ''}
                      {call.recorded && !call.consented ? ' (بدون موافقة)' : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="archive-group">
            <h4>سجل الحالة</h4>
            <ol className="archive-events">
              {timeline.events.map((event, index) => (
                <li key={`${event.event_type}-${index}`}>
                  <span>{eventLabel(event.event_type)}</span>
                  <span className="archive-muted">{formatDate(event.occurred_at)}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </section>
  );
}

export function RequestArchive() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ArchivePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  // Guards against an older, slower response overwriting a newer one when
  // someone types quickly.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      const data = await corporateApi.archive({
        search,
        status,
        source,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      if (seq !== requestSeq.current) return;
      setPage(data);
      setError('');
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(caught instanceof ApiError ? caught.message : 'تعذر تحميل الأرشيف.');
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [search, status, source, dateFrom, dateTo, offset]);

  useEffect(() => {
    let active = true;
    // Typing in the search box should not fire a request per keystroke.
    const timer = setTimeout(() => {
      if (active) void load();
    }, 250);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [load]);

  // Any filter change puts the reader back on the first page: staying on page
  // four of a result set that no longer has four pages shows nothing.
  const resetPage = useCallback(() => setOffset(0), []);

  const items = page?.items ?? [];
  const total = page?.total ?? 0;
  const summary = page?.summary;
  const hasFilters = Boolean(search || status || source || dateFrom || dateTo);

  const pageLabel = useMemo(() => {
    if (total === 0) return 'لا توجد نتائج';
    const first = offset + 1;
    const last = Math.min(offset + PAGE_SIZE, total);
    return `${first}–${last} من ${total}`;
  }, [offset, total]);

  return (
    <div className="corporate-archive">
      <section className="corporate-section">
        <h2>أرشيف الطلبات</h2>
        <p className="archive-intro">
          كل طلب تقدّم به موظفو الشركة، بما في ذلك الطلبات المغلقة، مع المراسلات والمرفقات
          والمكالمات المرتبطة به.
        </p>

        <div className="archive-filters">
          <label className="archive-field">
            <span>بحث</span>
            <input
              type="search"
              value={search}
              placeholder="اسم موظف، رقم موبايل، رقم طلب، وثيقة أو كارنيه"
              onChange={(event) => {
                setSearch(event.target.value);
                resetPage();
              }}
            />
          </label>

          <label className="archive-field">
            <span>الحالة</span>
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                resetPage();
              }}
            >
              <option value="">كل الحالات</option>
              {STATUS_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ))}
            </select>
          </label>

          <label className="archive-field">
            <span>المصدر</span>
            <select
              value={source}
              onChange={(event) => {
                setSource(event.target.value);
                resetPage();
              }}
            >
              <option value="">كل المصادر</option>
              {SOURCE_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {sourceLabel(value)}
                </option>
              ))}
            </select>
          </label>

          <label className="archive-field">
            <span>من تاريخ</span>
            <input
              type="date"
              value={dateFrom}
              onChange={(event) => {
                setDateFrom(event.target.value);
                resetPage();
              }}
            />
          </label>

          <label className="archive-field">
            <span>إلى تاريخ</span>
            <input
              type="date"
              value={dateTo}
              onChange={(event) => {
                setDateTo(event.target.value);
                resetPage();
              }}
            />
          </label>

          {hasFilters && (
            <button
              type="button"
              className="button button-light archive-clear"
              onClick={() => {
                setSearch('');
                setStatus('');
                setSource('');
                setDateFrom('');
                setDateTo('');
                resetPage();
              }}
            >
              مسح عوامل التصفية
            </button>
          )}
        </div>

        {summary && total > 0 && (
          <div className="archive-summary" aria-label="ملخص النتائج">
            <span>
              إجمالي النتائج: <strong>{total}</strong>
            </span>
            <span>
              المفتوحة: <strong>{summary.open}</strong>
            </span>
            {summary.first_at && <span>الأقدم: {formatDate(summary.first_at)}</span>}
            {summary.last_at && <span>الأحدث: {formatDate(summary.last_at)}</span>}
          </div>
        )}

        {error && (
          <div className="corporate-error" role="alert">
            {error}
          </div>
        )}
        {loading && (
          <div className="ds-loading-lines" aria-busy="true" aria-label="جارٍ تحميل الأرشيف">
            <span /><span /><span /><span /><span />
          </div>
        )}
        {!loading && !error && items.length === 0 && (
          <Blank
            icon={<Icon name="inbox" size={22} />}
            title="لا توجد طلبات مطابقة"
            body="جرّب توسيع الفترة الزمنية أو إزالة فلتر الحالة والمصدر."
          />
        )}

        {items.length > 0 && (
          <table className="archive-table">
            <thead>
              <tr>
                <th scope="col">رقم الطلب</th>
                <th scope="col">الموظف</th>
                <th scope="col">الموضوع</th>
                <th scope="col">المصدر</th>
                <th scope="col">الحالة</th>
                <th scope="col">التاريخ</th>
                <th scope="col">المرفقات</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {items.map((request) => (
                <tr key={request.id}>
                  <td dir="ltr" className="archive-ticket">
                    {request.ticket_no}
                  </td>
                  <td>
                    {request.employee_name ?? '—'}
                    {request.employee_mobile && (
                      <span className="archive-muted" dir="ltr">
                        {' '}
                        {request.employee_mobile}
                      </span>
                    )}
                  </td>
                  <td>{request.subject ?? '—'}</td>
                  <td>{sourceLabel(request.intake_source)}</td>
                  <td>
                    <span className={`archive-status is-${request.status}`}>
                      {statusLabel(request.status)}
                    </span>
                  </td>
                  <td>{formatDate(request.created_at)}</td>
                  <td>
                    {request.attachments_count > 0 ? request.attachments_count : '—'}
                    {request.calls_count > 0 ? ` · ${request.calls_count} مكالمة` : ''}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button button-light"
                      onClick={() => setOpenId(openId === request.id ? null : request.id)}
                    >
                      {openId === request.id ? 'إخفاء' : 'عرض'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {total > PAGE_SIZE && (
          <div className="archive-pager">
            <button
              type="button"
              className="button button-light"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              السابق
            </button>
            <span className="archive-page-label">{pageLabel}</span>
            <button
              type="button"
              className="button button-light"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              التالي
            </button>
          </div>
        )}
      </section>

      {openId && <TimelinePanel requestId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}
