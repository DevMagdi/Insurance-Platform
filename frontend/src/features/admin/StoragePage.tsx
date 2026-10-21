import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, queryString } from '../../api';
import { AccessDenied } from '../../components/Access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { formatDateTime, formatNumber } from '../../utils/date';
import { hasPermission } from '../../utils/access';
import type { StaffAccess } from '../../types';

type StorageKind = 'attachment' | 'export' | 'recording';

interface StorageObject {
  key: string;
  kind: string;
  size_bytes: number;
  last_modified: string | null;
  checksum_sha256: string | null;
  details: Record<string, unknown>;
}

interface KindTotals {
  objects: number;
  bytes: number;
  attention: number;
  missing_content: number;
}

interface StorageTotals {
  objects: number;
  bytes: number;
  pending_scans: number;
  attachment_metadata_without_content: number;
  by_kind: Record<string, KindTotals>;
}

interface StorageListing {
  backend: string;
  store: string;
  status: 'online';
  prefix: string;
  folders: string[];
  objects: StorageObject[];
  totals: StorageTotals;
  is_truncated: boolean;
  next_page_token: string | null;
}

interface StoragePageProps {
  access: StaffAccess;
}

const KIND_LABELS: Record<StorageKind | string, string> = {
  attachment: 'مرفق',
  export: 'تصدير تقرير',
  recording: 'تسجيل مكالمة',
};

const DETAIL_LABELS: Record<string, string> = {
  filename: 'اسم الملف',
  mime_type: 'نوع المحتوى',
  declared_size_bytes: 'الحجم المعلن',
  scan_status: 'حالة الفحص الأمني',
  visibility: 'نطاق الظهور',
  encrypted: 'محتوى مشفَّر',
  complaint_id: 'رقم الطلب',
  ticket_no: 'رقم التذكرة',
  export_kind: 'نوع التصدير',
  format: 'الصيغة',
  status: 'الحالة',
  owner_staff_id: 'الموظف المالك',
  expires_at: 'ينتهي في',
  direction: 'اتجاه المكالمة',
  provider: 'المزوّد',
  transcript_available: 'نص المكالمة متاح',
};

const BYTE_UNITS = ['كيلوبايت', 'ميجابايت', 'جيجابايت', 'تيرابايت'];

function canBrowseStorage(access: StaffAccess): boolean {
  return hasPermission(access, 'storage.manage')
    && access.all_companies
    && access.company_ids.length === 0
    && access.insurance_types.length === 0
    && access.ownership === 'all';
}

function folderName(path: string): string {
  const segments = path.split('/').filter(Boolean);
  return segments[segments.length - 1] ?? path;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${formatNumber(bytes)} بايت`;
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < BYTE_UNITS.length - 1);
  return `${formatNumber(value, 2)} ${BYTE_UNITS[unit]}`;
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

function objectTitle(object: StorageObject): string {
  const filename = object.details?.filename;
  if (typeof filename === 'string' && filename.trim()) return filename;
  return folderName(object.key);
}

function detailValue(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'نعم' : 'لا';
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'string') return value;
  return String(value);
}

function detailEntries(object: StorageObject): Array<[string, string]> {
  return Object.entries(object.details ?? {})
    .filter(([key]) => key in DETAIL_LABELS)
    .map(([key, value]) => [DETAIL_LABELS[key], detailValue(value)]);
}

function listingPath(prefix: string, token: string | null = null): string {
  return `/api/staff/storage${queryString({ prefix, continuation_token: token, limit: 100 })}`;
}

export function StoragePage({ access }: StoragePageProps) {
  const allowed = canBrowseStorage(access);
  const [prefix, setPrefix] = useState('');
  const [listing, setListing] = useState<StorageListing | null>(null);
  const [search, setSearch] = useState('');
  const [selectedObject, setSelectedObject] = useState<StorageObject | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refreshedAt, setRefreshedAt] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const requestId = useRef(0);

  const loadFirstPage = useCallback(async () => {
    const currentRequest = ++requestId.current;
    if (!allowed) {
      setLoading(false);
      setListing(null);
      return;
    }
    setLoading(true);
    setLoadingMore(false);
    setError('');
    setNotice('');
    setSelectedObject(null);
    try {
      const result = await api<StorageListing>(listingPath(prefix));
      if (currentRequest !== requestId.current) return;
      setListing(result);
      setRefreshedAt(new Date().toISOString());
    } catch (cause) {
      if (currentRequest !== requestId.current) return;
      setListing(null);
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل مخزون الملفات.');
    } finally {
      if (currentRequest === requestId.current) setLoading(false);
    }
  }, [allowed, prefix]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) void loadFirstPage();
    });
    return () => {
      active = false;
      requestId.current += 1;
    };
  }, [loadFirstPage, refreshKey]);

  const loadMore = async () => {
    if (!allowed || !listing?.next_page_token || loadingMore) return;
    const currentRequest = requestId.current;
    setLoadingMore(true);
    setError('');
    try {
      const result = await api<StorageListing>(listingPath(prefix, listing.next_page_token));
      if (currentRequest !== requestId.current) return;
      setListing((current) => {
        if (!current || current.prefix !== result.prefix) return current;
        return {
          ...result,
          folders: [...new Set([...current.folders, ...result.folders])],
          objects: [...current.objects, ...result.objects],
        };
      });
      setRefreshedAt(new Date().toISOString());
    } catch (cause) {
      if (currentRequest === requestId.current) {
        setError(cause instanceof Error ? cause.message : 'تعذر تحميل المزيد من الصفوف.');
      }
    } finally {
      if (currentRequest === requestId.current) setLoadingMore(false);
    }
  };

  const breadcrumbs = useMemo(() => {
    const segments = prefix.split('/').filter(Boolean);
    const result = [{ label: listing?.store ?? 'المخزون', path: '' }];
    let path = '';
    for (const segment of segments) {
      path += `${segment}/`;
      result.push({ label: segment, path });
    }
    return result;
  }, [listing?.store, prefix]);

  const visibleFolders = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return (listing?.folders ?? []).filter((folder) => folderName(folder).toLocaleLowerCase().includes(needle));
  }, [listing?.folders, search]);
  const visibleObjects = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return (listing?.objects ?? []).filter((object) => {
      const haystack = `${object.key} ${objectTitle(object)} ${object.kind}`.toLocaleLowerCase();
      return haystack.includes(needle);
    });
  }, [listing?.objects, search]);
  const displayedCount = visibleFolders.length + visibleObjects.length;
  const totals = listing?.totals;
  const pendingScans = totals?.pending_scans ?? 0;

  if (!allowed) {
    return <AccessDenied title="مركز التخزين غير متاح">
      يتطلب استعراض المخزون صلاحية التخزين ونطاق إدارة عام. تبقى تنزيلات الملفات ضمن مساراتها المصرّح بها.
    </AccessDenied>;
  }

  const navigateToPrefix = (path: string) => {
    setSearch('');
    setPrefix(path);
  };

  const copyObjectKey = async (key: string) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(key);
      setNotice('تم نسخ مسار الصف.');
    } catch {
      setNotice('تعذر النسخ تلقائياً؛ حدّد المسار وانسخه يدوياً.');
    }
  };

  return (
    <div className="page-stack storage-page">
      <SectionTitle
        eyebrow="الإدارة · الملفات الخاصة"
        title="مركز التخزين"
        description="مخزون داخلي يقرأ صفوف قاعدة بيانات المنصة: المرفقات وصادرات التقارير وتسجيلات المكالمات تُحفظ مشفَّرة داخل القاعدة بلا أي خدمة تخزين خارجية."
        action={(
          <button
            className="button button-light"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
            aria-label="تحديث مخزون الملفات"
          >
            <Icon name="refresh" size={15} /> تحديث
          </button>
        )}
      />

      {error && (
        <div className="inline-alert error" role="alert">
          {error}
          <button className="button button-quiet button-small" type="button" onClick={() => setRefreshKey((value) => value + 1)}>
            إعادة المحاولة
          </button>
        </div>
      )}
      {notice && (
        <div className="inline-alert info" role="status">
          {notice}
          <button className="notice-close" type="button" aria-label="إخفاء" onClick={() => setNotice('')}>
            <Icon name="close" size={14} />
          </button>
        </div>
      )}

      <section className="storage-overview surface-card" aria-label="حالة مخزون الملفات">
        <div className="storage-overview-main">
          <span className="storage-mark"><Icon name="layers" size={22} /></span>
          <div>
            <p className="eyebrow">قاعدة البيانات المُهيأة</p>
            <h3 dir="ltr">{loading && !listing ? 'جارٍ الاتصال…' : listing?.store ?? 'غير متاح'}</h3>
            <p>PostgreSQL · محتوى الملفات مشفَّر داخل الصفوف بمفتاح المنصة</p>
          </div>
        </div>
        <div className={`storage-health${listing?.status === 'online' ? ' is-online' : ''}`} role="status">
          <i /> {loading && !listing ? 'جارٍ التحقق' : listing?.status === 'online' ? 'متصل' : error ? 'تعذّر الاتصال' : 'بانتظار الاتصال'}
        </div>
      </section>

      <div className="storage-metrics">
        <article className="storage-metric surface-card">
          <span className="summary-stat-icon"><Icon name="paperclip" size={17} /></span>
          <div>
            <strong>{loading && !listing ? '—' : formatNumber(totals?.objects ?? 0)}</strong>
            <small>صفوف مخزَّنة في قاعدة البيانات</small>
          </div>
        </article>
        <article className="storage-metric surface-card">
          <span className="summary-stat-icon summary-stat-gold"><Icon name="layers" size={17} /></span>
          <div>
            <strong>{loading && !listing ? '—' : formatBytes(totals?.bytes ?? 0)}</strong>
            <small>حجم المحتوى المشفَّر</small>
          </div>
        </article>
        <article className="storage-metric storage-metric-note surface-card">
          <span className={`summary-stat-icon${pendingScans > 0 ? ' summary-stat-gold' : ' summary-stat-green'}`}>
            <Icon name="shield" size={17} />
          </span>
          <div>
            <strong>{loading && !listing ? '—' : pendingScans > 0 ? `${formatNumber(pendingScans)} بانتظار الفحص` : 'وصول محمي'}</strong>
            <small>
              {pendingScans > 0
                ? 'مرفقات لم يكتمل فحصها الأمني بعد'
                : 'لا يُعرض محتوى الملفات من لوحة المخزون'}
            </small>
          </div>
        </article>
      </div>

      <section className="storage-browser surface-card" aria-label="مستعرض صفوف المخزون">
        <div className="storage-browser-heading">
          <div className="storage-breadcrumbs" aria-label="مسار المخزون">
            {breadcrumbs.map((crumb, index) => (
              <span className="storage-crumb" key={`${crumb.path}:${index}`}>
                {index > 0 && <Icon name="chevron" size={13} />}
                <button
                  type="button"
                  className={index === breadcrumbs.length - 1 ? 'current' : ''}
                  aria-current={index === breadcrumbs.length - 1 ? 'page' : undefined}
                  onClick={() => navigateToPrefix(crumb.path)}
                  title={crumb.path || listing?.store}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          </div>
          <label className="storage-search">
            <Icon name="search" size={16} />
            <input
              aria-label="بحث في الصفحة المحمّلة"
              placeholder="بحث في النتائج المحمّلة…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {search && <button type="button" aria-label="مسح البحث" onClick={() => setSearch('')}><Icon name="close" size={13} /></button>}
          </label>
        </div>

        <div className="storage-table-meta">
          <div>
            <strong>{loading ? 'جارٍ تحميل المخزون…' : `${formatNumber(displayedCount)} عنصر`}</strong>
            <span>{listing?.prefix ? `المجلد الحالي · ${listing.prefix}` : 'جذر المخزون'}</span>
          </div>
          <small>{refreshedAt ? `آخر تحديث ${formatDateTime(refreshedAt)}` : 'بيانات مباشرة عند فتح الصفحة'}</small>
        </div>

        {loading && !listing ? (
          <div className="loading-surface storage-loading"><span className="loading-spinner" /> جارٍ قراءة صفوف المخزون…</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table storage-table">
              <thead><tr><th>الاسم</th><th>النوع</th><th>الحجم</th><th>آخر تعديل</th><th>تفاصيل</th></tr></thead>
              <tbody>
                {visibleFolders.map((folder) => (
                  <tr key={`folder:${folder}`}>
                    <td>
                      <button className="storage-name-button" type="button" onClick={() => navigateToPrefix(folder)} aria-label={`فتح المجلد ${folderName(folder)}`}>
                        <span className="storage-entry-icon is-folder"><Icon name="layers" size={16} /></span>
                        <code dir="ltr">{folder}/</code>
                      </button>
                    </td>
                    <td><span className="storage-kind">مجلد منطقي</span></td>
                    <td>—</td><td>—</td>
                    <td><button className="button button-quiet button-small" type="button" onClick={() => navigateToPrefix(folder)}>فتح</button></td>
                  </tr>
                ))}
                {visibleObjects.map((object) => (
                  <tr key={`object:${object.key}`}>
                    <td>
                      <div className="storage-name-cell">
                        <span className="storage-entry-icon"><Icon name="paperclip" size={15} /></span>
                        <code dir="ltr" title={object.key}>{objectTitle(object)}</code>
                      </div>
                    </td>
                    <td><span className="storage-kind is-object">{kindLabel(object.kind)}</span></td>
                    <td className="storage-size" dir="ltr">{formatBytes(object.size_bytes)}</td>
                    <td className="date-cell">{object.last_modified ? formatDateTime(object.last_modified) : '—'}</td>
                    <td>
                      <button className="button button-quiet button-small" type="button" onClick={() => setSelectedObject(object)} aria-label={`تفاصيل ${object.key}`}>
                        التفاصيل
                      </button>
                    </td>
                  </tr>
                ))}
                {!loading && displayedCount === 0 && (
                  <tr><td colSpan={5}>
                    <div className="empty-table storage-empty">
                      <span className="empty-icon"><Icon name="layers" size={20} /></span>
                      <strong>{search ? 'لا توجد نتائج مطابقة في الصفحة' : 'هذا المجلد فارغ'}</strong>
                      <span>{search ? 'يقتصر البحث على النتائج المحمّلة حالياً.' : 'ستظهر الصفوف المخزَّنة هنا عند توفرها.'}</span>
                    </div>
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {listing?.is_truncated && listing.next_page_token && (
          <div className="storage-load-more">
            <span>تظهر النتائج على دفعات للحفاظ على استجابة المخزون.</span>
            <button className="button button-light" type="button" onClick={() => { void loadMore(); }} disabled={loadingMore}>
              <Icon name={loadingMore ? 'refresh' : 'plus'} size={14} />
              {loadingMore ? 'جارٍ التحميل…' : 'تحميل المزيد'}
            </button>
          </div>
        )}
      </section>

      <p className="storage-safety-note"><Icon name="shield" size={14} />
        هذه لوحة مخزون داخلية للقراءة فقط. تنزيل المرفقات يخضع لصلاحيات الطلب ونتيجة الفحص الأمني من مسارها المعتاد؛ لا يمكن حذف الملفات أو تعديلها من هنا.
      </p>

      {selectedObject && (
        <Modal title="تفاصيل الصف" eyebrow="بيانات وصفية فقط · المحتوى المشفَّر محمي" onClose={() => setSelectedObject(null)}>
          <div className="storage-object-detail">
            <span className="storage-entry-icon"><Icon name="paperclip" size={17} /></span>
            <div><small>المسار المنطقي داخل المخزون</small><code dir="ltr">{selectedObject.key}</code></div>
          </div>
          <dl className="storage-detail-grid">
            <div><dt>النوع</dt><dd>{kindLabel(selectedObject.kind)}</dd></div>
            <div><dt>الحجم</dt><dd>{formatBytes(selectedObject.size_bytes)}</dd></div>
            <div><dt>آخر تعديل</dt><dd>{selectedObject.last_modified ? formatDateTime(selectedObject.last_modified) : '—'}</dd></div>
            <div><dt>بصمة SHA-256</dt><dd dir="ltr">{selectedObject.checksum_sha256 ?? '—'}</dd></div>
            {detailEntries(selectedObject).map(([label, value], index) => (
              <div key={`detail:${index}:${label}`}><dt>{label}</dt><dd dir="auto">{value}</dd></div>
            ))}
          </dl>
          <p className="modal-intro">يبقى محتوى الملفات مشفَّراً داخل قاعدة البيانات، ولا يُسلَّم إلا عبر مسارات التنزيل المصرّح بها.</p>
          <div className="modal-actions">
            <button className="button button-light" type="button" onClick={() => { void copyObjectKey(selectedObject.key); }}>نسخ المسار</button>
            <button className="button button-dark" type="button" onClick={() => setSelectedObject(null)}>تم</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
