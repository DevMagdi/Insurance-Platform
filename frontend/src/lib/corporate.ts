/**
 * Corporate client portal API.
 *
 * The second interface of the platform: a company contracted with the
 * brokerage signs in with its own username and password and sees only its
 * own contract, its employees' requests and the documents shared with it.
 * Every shape here was taken from the live `/api/corporate/*` responses.
 */
import { ApiError } from '../api';
import { formatNumber } from '../utils/date';
import { getCsrfToken } from './session';

export type PaymentFrequency = 'monthly' | 'quarterly' | 'semi_annual' | 'annual';
export type ContractStatus = 'draft' | 'active' | 'expired' | 'cancelled';

export interface CorporateClient {
  id: string;
  slug: string;
  name_ar: string;
  name_en: string;
  address: string | null;
  contact_name: string | null;
  contact_email: string | null;
  contact_mobile: string | null;
}

export interface CorporateDocument {
  id: string;
  title: string;
  kind: string | null;
  file_name: string | null;
  content_type: string | null;
  byte_size: number | null;
  uploaded_at: string | null;
}

export interface CorporateContract {
  id: string;
  contract_no: string;
  contract_value: number;
  currency: string;
  payment_frequency: PaymentFrequency;
  starts_on: string;
  renews_on: string;
  status: ContractStatus;
  headcount: number | null;
  /** Server-computed, so the countdown never depends on the browser clock. */
  days_to_renewal: number | null;
  documents: CorporateDocument[];
}

export interface CorporatePolicy {
  id: string;
  policy_number: string | null;
  insurance_type: string | null;
  company_name: string | null;
  starts_on: string | null;
  ends_on: string | null;
  documents?: CorporateDocument[];
}

export interface CorporateOverview {
  client: CorporateClient;
  policies: CorporatePolicy[];
  /** A company may hold several contracts with the broker at once. */
  contracts: CorporateContract[];
  requests_open: number;
  requests_total: number;
  employees_count: number;
}

export interface CorporateMemberRequest {
  id: string;
  ticket_no: string;
  subject: string | null;
  status: string;
  channel: string | null;
  intake_source: string | null;
  policy_number: string | null;
  card_number: string | null;
  created_at: string;
  updated_at: string;
}

export interface CorporateMember {
  id: string;
  full_name: string;
  mobile: string | null;
  email: string | null;
  employee_code: string | null;
  requests_count: number;
  requests: CorporateMemberRequest[];
}

export interface CorporateMemberList {
  items: CorporateMember[];
  total: number;
}

export interface CorporateDocumentList {
  items: CorporateDocument[];
  total: number;
}

/** One row of the archive: a request plus what is attached to it. */
export interface ArchiveRequest {
  id: string;
  ticket_no: string;
  subject: string | null;
  status: string;
  priority: string | null;
  channel: string | null;
  intake_source: string | null;
  policy_number: string | null;
  card_number: string | null;
  employee_id: string | null;
  employee_name: string | null;
  employee_code: string | null;
  employee_mobile: string | null;
  messages_count: number;
  attachments_count: number;
  calls_count: number;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
}

export interface ArchiveSummary {
  by_status: Record<string, number>;
  by_source: Record<string, number>;
  open: number;
  first_at: string | null;
  last_at: string | null;
}

export interface ArchivePage {
  items: ArchiveRequest[];
  total: number;
  summary: ArchiveSummary;
  limit: number;
  offset: number;
}

export interface ArchiveFilters {
  search?: string;
  status?: string;
  source?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
  offset?: number;
}

export interface TimelineMessage {
  id: string;
  direction: string;
  channel: string | null;
  created_at: string;
  body: string | null;
  delivery_status: string | null;
}

export interface TimelineAttachment {
  id: string;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  scan_status: string | null;
  created_at: string;
}

export interface TimelineCall {
  id: string;
  direction: string;
  provider: string | null;
  outcome: string | null;
  status: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  recorded: boolean;
  consented: boolean;
}

export interface TimelineEvent {
  event_type: string;
  occurred_at: string;
  from_status: string | null;
  to_status: string | null;
}

export interface RequestTimeline {
  id: string;
  ticket_no: string;
  subject: string | null;
  status: string;
  priority: string | null;
  channel: string | null;
  intake_source: string | null;
  policy_number: string | null;
  card_number: string | null;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
  employee: {
    id: string;
    full_name: string;
    mobile: string | null;
    email: string | null;
    employee_code: string | null;
  } | null;
  messages: TimelineMessage[];
  attachments: TimelineAttachment[];
  calls: TimelineCall[];
  events: TimelineEvent[];
}

export interface CorporateLoginResult {
  next_step: 'authenticated' | 'change_password';
  full_name: string;
  must_change_password: boolean;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
}

async function corporateRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'include',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let raw: unknown = detail;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        raw = (body as { detail?: unknown }).detail;
        if (typeof raw === 'string') detail = raw;
      }
    } catch {
      // Non-JSON proxy/provider failure: keep the status-based message.
    }
    throw new ApiError(response.status, detail, describeCorporateError(response.status, detail), raw);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Arabic messages for the handful of failures this portal can produce. */
function describeCorporateError(status: number, detail: string): string {
  if (status === 401) {
    return detail.toLowerCase().includes('expired')
      ? 'انتهت الجلسة. سجّل الدخول مرة أخرى.'
      : 'اسم المستخدم أو كلمة المرور غير صحيحة.';
  }
  if (status === 403) return 'لا تملك صلاحية الوصول إلى هذه البيانات.';
  if (status === 404) return 'العنصر المطلوب غير متاح.';
  if (status === 423 || status === 429) {
    return 'تم إيقاف المحاولات مؤقتاً بعد عدة محاولات فاشلة. انتظر قليلاً ثم أعد المحاولة.';
  }
  if (status >= 500) return 'تعذر الوصول إلى الخدمة حالياً. حاول مرة أخرى بعد قليل.';
  return 'تعذر إتمام الطلب.';
}

export const corporateApi = {
  login: (username: string, password: string, captcha?: string | null) =>
    corporateRequest<CorporateLoginResult>('/api/corporate/login', {
      method: 'POST',
      body: { username: username.trim().toLowerCase(), password },
      headers: captcha ? { 'X-Captcha-Token': captcha } : {},
    }),

  changePassword: (currentPassword: string, newPassword: string) =>
    corporateRequest<{ next_step: string }>('/api/corporate/password/change', {
      method: 'POST',
      body: { current_password: currentPassword, new_password: newPassword },
    }),

  overview: () => corporateRequest<CorporateOverview>('/api/corporate/overview'),

  /** `search` matches employee name, employee code, or mobile digits. */
  members: (search?: string) => {
    const term = (search ?? '').trim();
    const query = term ? `?search=${encodeURIComponent(term)}` : '';
    return corporateRequest<CorporateMemberList>(`/api/corporate/members${query}`);
  },

  documents: () => corporateRequest<CorporateDocumentList>('/api/corporate/documents'),

  /**
   * The whole request history, filtered and paged. Empty filters are dropped
   * rather than sent blank, so the URL reads as what the user actually chose.
   */
  archive: (filters: ArchiveFilters = {}) => {
    const query = new URLSearchParams();
    const search = (filters.search ?? '').trim();
    if (search) query.set('search', search);
    if (filters.status) query.set('status', filters.status);
    if (filters.source) query.set('source', filters.source);
    if (filters.dateFrom) query.set('date_from', filters.dateFrom);
    if (filters.dateTo) query.set('date_to', filters.dateTo);
    query.set('limit', String(filters.limit ?? 25));
    query.set('offset', String(filters.offset ?? 0));
    return corporateRequest<ArchivePage>(`/api/corporate/archive?${query.toString()}`);
  },

  requestTimeline: (requestId: string) =>
    corporateRequest<RequestTimeline>(`/api/corporate/archive/${requestId}`),

  documentUrl: (documentId: string) => `/api/corporate/documents/${documentId}`,
};

export const PAYMENT_FREQUENCY_LABEL: Record<PaymentFrequency, string> = {
  monthly: 'شهري',
  quarterly: 'ربع سنوي',
  semi_annual: 'نصف سنوي',
  annual: 'سنوي',
};

export const CONTRACT_STATUS_LABEL: Record<ContractStatus, string> = {
  draft: 'مسودة',
  active: 'ساري',
  expired: 'منتهي',
  cancelled: 'ملغي',
};

export const REQUEST_STATUS_LABEL: Record<string, string> = {
  new: 'جديد',
  assigned: 'تم التعيين',
  in_review: 'قيد المراجعة',
  resolved: 'تم الحل',
  reopened: 'أُعيد فتحه',
  transferred: 'محوّل',
};

export const INTAKE_SOURCE_LABEL: Record<string, string> = {
  whatsapp: 'واتساب',
  phone: 'مكالمة هاتفية',
  email: 'بريد إلكتروني',
  sms: 'رسالة نصية',
  portal: 'البوابة',
  staff: 'إدخال موظف',
  corporate: 'بوابة الشركة',
};

/**
 * Money reuses the platform's existing `ar-EG` number helper so the portal
 * reads identically to the rest of the Arabic UI instead of introducing a
 * second number style.
 */
export function formatMoney(value: number, currency: string): string {
  return `${formatNumber(value, 2)} ${currency}`;
}

/** Contract dates are day-precision; the shared helper would add a time. */
export function formatDate(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ar-EG', { dateStyle: 'medium' }).format(date);
}

/**
 * Renewal wording. The server sends the day count, so an expired or
 * same-day contract reads correctly instead of showing "-3 يوم".
 */
export function renewalNotice(days: number | null): { text: string; tone: 'calm' | 'warn' | 'alert' } {
  if (days === null || Number.isNaN(days)) return { text: 'غير محدد', tone: 'calm' };
  if (days < 0) return { text: `انتهى منذ ${Math.abs(days)} يوم`, tone: 'alert' };
  if (days === 0) return { text: 'يُجدَّد اليوم', tone: 'alert' };
  if (days === 1) return { text: 'يُجدَّد غداً', tone: 'alert' };
  if (days <= 30) return { text: `يُجدَّد خلال ${days} يوم`, tone: 'warn' };
  return { text: `يُجدَّد خلال ${days} يوم`, tone: 'calm' };
}

/** A call length reads as minutes and seconds, never as a raw count. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds) || seconds < 0) return '—';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${formatNumber(rest)} ثانية`;
  return `${formatNumber(minutes)}:${String(rest).padStart(2, '0')} دقيقة`;
}

/** Attachment sizes: the archive lists them before anyone downloads them. */
export function formatBytes(size: number | null): string {
  if (size === null || Number.isNaN(size) || size < 0) return '—';
  if (size < 1024) return `${formatNumber(size)} بايت`;
  if (size < 1024 * 1024) return `${formatNumber(Math.round(size / 1024))} ك.ب`;
  return `${formatNumber(Math.round((size / (1024 * 1024)) * 10) / 10, 1)} م.ب`;
}

export const EVENT_TYPE_LABEL: Record<string, string> = {
  created: 'تم إنشاء الطلب',
  auto_acknowledged: 'تم إرسال إشعار الاستلام تلقائياً',
  auto_closed: 'أُغلق تلقائياً لعدم وجود رد',
  status_changed: 'تغيّرت الحالة',
  assigned: 'تم التعيين',
  reopened: 'أُعيد فتح الطلب',
  inbound_voice_call: 'مكالمة هاتفية واردة',
  resolved: 'تم الحل',
};
