/** Same-origin `/api` client. The browser never talks to a backend host directly. */
import type {
  AttachmentRecord,
  ComplaintCounters,
  ComplaintCreateBody,
  ComplaintDetail,
  ComplaintFilters,
  ComplaintListResponse,
  ComplaintTimeline,
  Company,
  CustomerNotification,
  CustomerProfile,
  GuestTrackResult,
  MeResponse,
  UploadReservation,
} from './types';
import {
  staffComplaintsQuery,
  type AiInsight,
  type AnalyticsOverview,
  type CannedReply,
  type ExportJob,
  type IntegrationJob,
  type IntegrationJobQueued,
  type SavedView,
  type StaffComplaintFilters,
  type StaffFilterOptions,
  type StaffNotification,
  type StaffRole,
  type StaffUser,
} from './staffTypes';
import type { StaffDepartment } from '../types';
import { captchaHeaders } from './captcha';
import {
  getCsrfToken,
  notifyAuthExpired,
  refreshSession,
  shouldAttemptRefresh,
} from './session';

export type ConsentPurpose =
  | 'complaint_processing'
  | 'notifications'
  | 'voice_followup'
  | 'ai_processing'
  | 'marketing';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let detail = response.statusText || 'Request failed';
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const value = (body as { detail: unknown }).detail;
      if (typeof value === 'string') detail = value;
    }
  } catch {
    // Non-JSON error body; keep the status text.
  }
  return new ApiError(response.status, detail);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  /** Internal: a 401 may retry exactly once after a token refresh. */
  retried?: boolean;
}

async function send(path: string, options: RequestOptions = {}): Promise<Response> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { ...options.headers };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }

  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401 && shouldAttemptRefresh(path)) {
    if (!options.retried && await refreshSession()) {
      return send(path, { ...options, retried: true });
    }
    notifyAuthExpired();
  }
  return response;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options);
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requestText(path: string, options: RequestOptions = {}): Promise<string> {
  const response = await send(path, options);
  if (!response.ok) throw await parseError(response);
  return response.text();
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function queryString(filters: ComplaintFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}

export const api = {
  companies: () => request<Company[]>('/api/companies'),

  guestTrack: (ticket_no: string, national_id: string, captcha?: string | null) =>
    request<GuestTrackResult>('/api/guest/track', {
      method: 'POST',
      body: { ticket_no: ticket_no.trim(), national_id: national_id.replace(/[\s-]/g, '') },
      headers: captchaHeaders(captcha),
    }),

  otpStart: (mobile: string, captcha?: string | null) =>
    request<{ message: string }>('/api/auth/customer/otp/start', {
      method: 'POST',
      body: { mobile },
      headers: captchaHeaders(captcha),
    }),

  /** PDPL (Law 151/2020): append-only consent record for the signed-in customer. */
  recordConsent: (purpose: ConsentPurpose, granted: boolean) =>
    request<{ purpose: ConsentPurpose; granted: boolean }>('/api/customer/privacy/consents', {
      method: 'POST',
      body: { purpose, granted },
    }),

  otpVerify: (body: {
    mobile: string;
    code: string;
    national_id?: string;
    full_name?: string;
    email?: string;
    whatsapp_available?: boolean;
  }) => request<{ authenticated_as: string }>('/api/auth/customer/otp/verify', { method: 'POST', body }),

  logout: () => request<{ logged_out: boolean }>('/api/auth/logout', { method: 'POST' }),

  me: () => request<MeResponse>('/api/me'),

  profile: () => request<CustomerProfile>('/api/customer/profile'),

  updateProfile: (body: {
    full_name: string;
    email: string | null;
    whatsapp_available: boolean;
    notification_preferences: { email: boolean; whatsapp: boolean; sms: boolean };
  }) => request<CustomerProfile>('/api/customer/profile', { method: 'PATCH', body }),

  createComplaint: (body: ComplaintCreateBody, idempotencyKey: string) =>
    request<ComplaintDetail & { replayed: boolean }>('/api/customer/complaints', {
      method: 'POST',
      body,
      headers: { 'Idempotency-Key': idempotencyKey },
    }),

  listComplaints: (filters: ComplaintFilters) =>
    request<ComplaintListResponse>(`/api/customer/complaints${queryString(filters)}`),

  counters: (filters: ComplaintFilters) =>
    request<ComplaintCounters>(`/api/customer/complaints/counters${queryString(filters)}`),

  complaint: (id: string) => request<ComplaintDetail>(`/api/customer/complaints/${id}`),

  timeline: (id: string) =>
    request<ComplaintTimeline>(`/api/customer/complaints/${id}/timeline?page=1&page_size=100`),

  attachments: (id: string) =>
    request<AttachmentRecord[]>(`/api/customer/complaints/${id}/attachments`),

  planUploads: (complaintId: string, files: { filename: string; mime_type: string; size_bytes: number; checksum_sha256: string }[]) =>
    request<{ uploads: UploadReservation[] }>(
      `/api/customer/complaints/${complaintId}/attachments/plan`,
      { method: 'POST', body: { files } },
    ),

  reply: (id: string, body: string) =>
    request<{ id: string; body: string }>(`/api/customer/complaints/${id}/reply`, {
      method: 'POST',
      body: { body },
    }),

  reopen: (id: string) =>
    request<{ status: string }>(`/api/customer/complaints/${id}/reopen`, { method: 'POST' }),

  csat: (id: string, score: number, comment: string) =>
    request<{ score: number; submitted: boolean }>(`/api/customer/complaints/${id}/csat`, {
      method: 'POST',
      body: { score, comment: comment.trim() ? comment.trim() : null },
    }),

  notifications: () => request<CustomerNotification[]>('/api/customer/notifications'),

  markNotificationRead: (id: string) =>
    request<{ read: boolean }>(`/api/customer/notifications/${id}/read`, { method: 'PATCH' }),

  authorizeDownload: (attachmentId: string) =>
    request<{ download_url: string; download_token: string; expires_in_seconds: number }>(
      `/api/customer/attachments/${attachmentId}/authorize`,
    ),

  /** Download through the scoped API so the one-use token never lands in a URL. */
  downloadAttachment: async (attachmentId: string, filename: string): Promise<void> => {
    const grant = await api.authorizeDownload(attachmentId);
    const response = await send(grant.download_url, {
      headers: { 'X-Download-Token': grant.download_token },
    });
    if (!response.ok) throw await parseError(response);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  },
};

export const staffApi = {
  login: (email: string, password: string, captcha?: string | null) =>
    request<{ next_step: string; challenge: string | null }>('/api/auth/staff/login', {
      method: 'POST',
      body: { email, password },
      headers: captchaHeaders(captcha),
    }),

  changePassword: (challenge: string, password: string) =>
    request<{ next_step: string }>('/api/auth/staff/password/change', {
      method: 'POST',
      body: { challenge, password },
    }),

  totpVerify: (challenge: string, code: string) =>
    request<{ authenticated_as: string }>('/api/auth/staff/2fa/verify', {
      method: 'POST',
      body: { challenge, code },
    }),

  totpEnrollStart: (challenge: string) =>
    request<{ provisioning_uri: string }>('/api/auth/staff/2fa/enroll/start', {
      method: 'POST',
      body: { challenge },
    }),

  totpEnrollConfirm: (challenge: string, code: string) =>
    request<{ next_step: string }>('/api/auth/staff/2fa/enroll/confirm', {
      method: 'POST',
      body: { challenge, code },
    }),

  complaints: (filters: StaffComplaintFilters) =>
    request<ComplaintListResponse>(`/api/staff/complaints${staffComplaintsQuery(filters)}`),

  counters: (filters: StaffComplaintFilters) =>
    request<ComplaintCounters>(`/api/staff/complaints/counters${staffComplaintsQuery(filters)}`),

  filterOptions: () => request<StaffFilterOptions>('/api/staff/complaints/filter-options'),

  complaint: (id: string) => request<ComplaintDetail>(`/api/staff/complaints/${id}`),

  timeline: (id: string) =>
    request<ComplaintTimeline>(`/api/staff/complaints/${id}/timeline?page=1&page_size=100`),

  attachments: (id: string) => request<AttachmentRecord[]>(`/api/staff/complaints/${id}/attachments`),

  reply: (id: string, body: string) =>
    request<{ id: string; body: string }>(`/api/staff/complaints/${id}/reply`, {
      method: 'POST',
      body: { body },
    }),

  note: (id: string, body: string) =>
    request<{ id: string; body: string }>(`/api/staff/complaints/${id}/notes`, {
      method: 'POST',
      body: { body },
    }),

  status: (id: string, status: string) =>
    request<{ status: string }>(`/api/staff/complaints/${id}/status`, {
      method: 'PATCH',
      body: { status },
    }),

  revealNationalId: (id: string, reason: string) =>
    request<{ national_id: string }>(`/api/staff/complaints/${id}/national-id:reveal`, {
      method: 'POST',
      body: { reason },
    }),

  assign: (id: string, agent_id: string, team_id?: string, reassign = false) =>
    request<Record<string, unknown>>(`/api/staff/complaints/${id}/${reassign ? 'reassign' : 'assign'}`, {
      method: 'POST',
      body: { agent_id, team_id: team_id || null },
    }),

  escalate: (id: string, reason: string) =>
    request<Record<string, unknown>>(`/api/staff/complaints/${id}/escalate`, {
      method: 'POST',
      body: { reason },
    }),

  merge: (id: string, target_id: string) =>
    request<Record<string, unknown>>(`/api/staff/complaints/${id}/merge`, {
      method: 'POST',
      body: { target_id },
    }),

  duplicates: (id: string) =>
    request<Record<string, unknown>[]>(`/api/staff/complaints/${id}/duplicates`),

  cannedReplies: (id: string) => request<CannedReply[]>(`/api/staff/complaints/${id}/canned-replies`),

  bulk: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>('/api/staff/complaints/bulk', { method: 'POST', body }),

  notifications: () => request<StaffNotification[]>('/api/staff/notifications'),

  markNotificationRead: (id: string) =>
    request<{ read: boolean }>(`/api/staff/notifications/${id}/read`, { method: 'PATCH' }),

  views: () => request<SavedView[]>('/api/staff/views'),

  createView: (name: string, criteria: StaffComplaintFilters) =>
    request<SavedView>('/api/staff/views', { method: 'POST', body: { name, criteria } }),

  deleteView: (id: string) => request<void>(`/api/staff/views/${id}`, { method: 'DELETE' }),

  aiInsights: (id: string) => request<AiInsight[]>(`/api/staff/complaints/${id}/ai/insights`),

  generateAi: (id: string, locale: 'ar' | 'en') =>
    request<IntegrationJobQueued>(`/api/staff/complaints/${id}/ai/analyze`, {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: { locale },
    }),

  integrationJob: (id: string) => request<IntegrationJob>(`/api/staff/integration-jobs/${id}`),

  reviewAi: (complaintId: string, insightId: string, accept: boolean) =>
    request<AiInsight>(`/api/staff/complaints/${complaintId}/ai/insights/${insightId}/review`, {
      method: 'POST',
      body: { accept },
    }),

  sendAi: (complaintId: string, insightId: string, channel: 'email' | 'whatsapp') =>
    request<IntegrationJobQueued>(
      `/api/staff/complaints/${complaintId}/ai/insights/${insightId}/send?channel=${channel}`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      },
    ),

  analytics: (filters: StaffComplaintFilters) =>
    request<AnalyticsOverview>(`/api/staff/analytics/overview${staffComplaintsQuery(filters)}`),

  scorecards: (filters: StaffComplaintFilters) =>
    request<Record<string, unknown>[]>(`/api/staff/analytics/scorecards${staffComplaintsQuery(filters)}`),

  users: () => request<StaffUser[]>('/api/staff/users'),

  departments: () => request<StaffDepartment[]>('/api/staff/departments'),

  createDepartment: (body: { name: string; description: string; is_active: boolean }) =>
    request<StaffDepartment>('/api/staff/departments', { method: 'POST', body }),

  updateDepartment: (id: string, body: { name: string; description: string; is_active: boolean }) =>
    request<Pick<StaffDepartment, 'id' | 'name' | 'description' | 'is_active'>>(
      `/api/staff/departments/${encodeURIComponent(id)}`, { method: 'PUT', body },
    ),

  inviteUser: (email: string, full_name: string, department_id: string) =>
    request<{ id: string; message: string }>('/api/staff/users/invitations', {
      method: 'POST',
      body: { email, full_name, department_id },
    }),

  userAccess: (id: string) => request<Record<string, unknown>>(`/api/staff/users/${id}/access`),

  explainAccess: (id: string) =>
    request<Record<string, unknown>>(`/api/staff/users/${id}/access/explain`),

  replaceAccess: (id: string, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/staff/users/${id}/access`, { method: 'PUT', body }),

  editUser: (id: string, email: string, full_name: string) =>
    request<Record<string, unknown>>(`/api/staff/users/${id}`, {
      method: 'PATCH',
      body: { email, full_name },
    }),

  deactivateUser: (id: string) =>
    request<{ deactivated: boolean }>(`/api/staff/users/${id}/deactivate`, { method: 'POST' }),

  reactivateUser: (id: string) =>
    request<{ reactivated: boolean }>(`/api/staff/users/${id}/reactivate`, { method: 'POST' }),

  forceLogoutUser: (id: string) =>
    request<{ sessions_revoked: boolean }>(`/api/staff/users/${id}/force-logout`, { method: 'POST' }),

  resetUserPassword: (id: string) =>
    request<{ message: string }>(`/api/staff/users/${id}/password-reset`, { method: 'POST' }),

  userTotpPolicy: (id: string, required: boolean) =>
    request<Record<string, unknown>>(`/api/staff/users/${id}/2fa-policy`, {
      method: 'PATCH',
      body: { required },
    }),

  globalTotpPolicy: (required: boolean) =>
    request<{ staff_2fa_required: boolean }>('/api/staff/settings/staff-2fa', {
      method: 'PATCH',
      body: { required },
    }),

  roles: () => request<StaffRole[]>('/api/staff/roles'),

  saveRole: (id: string | null, body: { name: string; description: string; permissions: string[] }) =>
    request<{ id: string }>(id ? `/api/staff/roles/${id}` : '/api/staff/roles', {
      method: id ? 'PUT' : 'POST',
      body,
    }),

  cloneRole: (id: string, name: string) =>
    request<{ id: string }>(`/api/staff/roles/${id}/clone`, { method: 'POST', body: { name } }),

  catalog: (kind: 'companies' | 'categories' | 'sla-rules' | 'templates') =>
    request<Record<string, unknown>[]>(`/api/staff/admin/${kind}`),

  saveCatalog: (
    kind: 'companies' | 'categories' | 'sla-rules' | 'templates',
    id: string | null,
    body: Record<string, unknown>,
  ) =>
    request<{ id: string }>(id ? `/api/staff/admin/${kind}/${id}` : `/api/staff/admin/${kind}`, {
      method: id ? 'PUT' : 'POST',
      body,
    }),

  auditEvents: (start: string, end: string) =>
    request<Record<string, unknown>[]>(`/api/staff/audit/events?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&limit=100`),

  accessChanges: () => request<Record<string, unknown>[]>('/api/staff/audit/access-changes?limit=100'),

  exports: () => request<ExportJob[]>('/api/staff/exports'),

  createExport: (kind: 'complaints' | 'analytics', filters: StaffComplaintFilters) =>
    request<{ id: string; status: string }>('/api/staff/exports', {
      method: 'POST',
      body: { kind, filters },
    }),

  downloadExport: async (id: string): Promise<void> => {
    const response = await send(`/api/staff/exports/${id}/download`);
    if (!response.ok) throw await parseError(response);
    saveBlob(await response.blob(), `insurance-export-${id}.csv`);
  },

  downloadComplaintsCsv: async (filters: StaffComplaintFilters): Promise<void> => {
    const text = await requestText(`/api/staff/complaints/export${staffComplaintsQuery(filters)}`);
    saveBlob(new Blob([text], { type: 'text/csv; charset=utf-8' }), 'complaints.csv');
  },

  downloadAuditCsv: async (start: string, end: string): Promise<void> => {
    const text = await requestText(
      `/api/staff/audit/events.csv?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    );
    saveBlob(new Blob([text], { type: 'text/csv; charset=utf-8' }), 'audit-events.csv');
  },

  downloadAttachment: async (attachmentId: string, filename: string): Promise<void> => {
    const grant = await request<{ download_url: string; download_token: string }>(
      `/api/staff/attachments/${attachmentId}/authorize`,
    );
    const response = await send(grant.download_url, {
      headers: { 'X-Download-Token': grant.download_token },
    });
    if (!response.ok) throw await parseError(response);
    saveBlob(await response.blob(), filename);
  },
};

/** SHA-256 hex digest required by the upload-plan contract. */
export async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** PUT one reserved upload with real progress events (fetch cannot report upload progress). */
export function uploadWithProgress(
  reservation: UploadReservation,
  file: File,
  onProgress: (percent: number) => void,
): Promise<{ id: string; scan_status: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', reservation.upload_url);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-Upload-Token', reservation.upload_token);
    xhr.setRequestHeader('Content-Type', file.type);
    const csrf = getCsrfToken();
    if (csrf) xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as { id: string; scan_status: string });
        } catch {
          reject(new ApiError(xhr.status, 'Invalid upload response'));
        }
      } else {
        let detail = xhr.statusText || 'Upload failed';
        try {
          const body: unknown = JSON.parse(xhr.responseText);
          if (body && typeof body === 'object' && 'detail' in body) {
            const value = (body as { detail: unknown }).detail;
            if (typeof value === 'string') detail = value;
          }
        } catch {
          // Keep status text.
        }
        reject(new ApiError(xhr.status, detail));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, 'Network error during upload'));
    xhr.send(file);
  });
}

/** Plan + upload a batch, reporting per-file progress. Returns per-file outcomes. */
export async function uploadFiles(
  complaintId: string,
  files: File[],
  onProgress: (fileIndex: number, percent: number) => void,
): Promise<{ file: File; ok: boolean; error?: string }[]> {
  const planned = await Promise.all(
    files.map(async (file) => ({
      filename: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      checksum_sha256: await sha256Hex(file),
    })),
  );
  const { uploads } = await api.planUploads(complaintId, planned);
  const results: { file: File; ok: boolean; error?: string }[] = [];
  for (let i = 0; i < files.length; i += 1) {
    try {
      await uploadWithProgress(uploads[i], files[i], (percent) => onProgress(i, percent));
      results.push({ file: files[i], ok: true });
    } catch (error) {
      results.push({
        file: files[i],
        ok: false,
        error: error instanceof Error ? error.message : 'Upload failed',
      });
    }
  }
  return results;
}
