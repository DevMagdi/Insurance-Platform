/** API contracts mirrored from the implemented FastAPI backend (`docs/openapi.yaml`). */

export type InsuranceType = 'health' | 'general' | 'both';

export type ComplaintStatus =
  | 'new'
  | 'assigned'
  | 'in_review'
  | 'resolved'
  | 'reopened'
  | 'transferred';

export interface Company {
  id: string;
  name_en: string;
  name_ar: string;
  insurance_types: InsuranceType[];
}

export interface ComplaintSummary {
  id: string;
  ticket_no: string;
  status: ComplaintStatus;
  company_id: string;
  insurance_type: InsuranceType;
  subject: string;
  priority: 'low' | 'normal' | 'high' | 'urgent';
  category_id: string | null;
  channel: string;
  assigned_agent_id: string | null;
  sla_due_at: string | null;
  response_due_at: string | null;
  sla_breached_at: string | null;
  merged_into_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ComplaintDetail extends ComplaintSummary {
  description: string;
  policy_number: string;
  customer_name: string;
  national_id_masked: string;
  company_name: string;
  assigned_agent_name: string | null;
  csat_score: number | null;
  can_reopen: boolean;
  can_csat: boolean;
  resolved_at: string | null;
  reopened_count: number;
}

export interface ComplaintListResponse {
  items: ComplaintSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface ComplaintCounters {
  total: number;
  open: number;
  closed: number;
  new: number;
  assigned: number;
  in_review: number;
  resolved: number;
  reopened: number;
  transferred: number;
}

export interface TimelineEvent {
  id: string;
  event_type: string;
  from_status: ComplaintStatus | null;
  to_status: ComplaintStatus | null;
  occurred_at: string;
}

export interface TimelineMessage {
  id: string;
  body: string;
  visibility: 'public' | 'internal';
  direction: 'inbound' | 'outbound';
  created_at: string;
}

export interface ComplaintTimeline {
  events: TimelineEvent[];
  messages: TimelineMessage[];
  page: number;
}

export interface AttachmentRecord {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  scan_status: 'pending' | 'clean' | 'infected' | 'error';
}

export interface UploadReservation {
  id: string;
  upload_url: string;
  upload_token: string;
  expires_at: string;
  size_bytes: number;
}

export interface NotificationPreferences {
  email: boolean;
  whatsapp: boolean;
  sms: boolean;
}

export interface CustomerProfile {
  id: string;
  full_name: string;
  mobile: string;
  email: string | null;
  whatsapp_available: boolean;
  national_id_masked: string;
  notification_preferences: NotificationPreferences;
}

export interface MeCustomer {
  kind: 'customer';
  id: string;
  full_name: string;
  mobile: string;
  email: string | null;
  national_id_masked: string;
}

export interface MeStaff {
  kind: 'staff';
  access: import('./staffTypes').StaffAccess;
}

export type MeResponse = MeCustomer | MeStaff;

export interface GuestTrackResult {
  public_ticket: string;
  public_status: ComplaintStatus;
  public_company: string;
  created: string;
}

export interface CustomerNotification {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  complaint_id: string | null;
  created_at: string;
  read_at: string | null;
}

export interface ComplaintFilters {
  status?: ComplaintStatus;
  company_id?: string;
  insurance_type?: InsuranceType;
  date_from?: string;
  date_to?: string;
  search?: string;
  ticket_no?: string;
  page?: number;
  page_size?: number;
  sort_by?: 'created_at' | 'updated_at';
  sort_dir?: 'asc' | 'desc';
}

export interface ComplaintCreateBody {
  national_id: string;
  mobile: string;
  company_id: string;
  insurance_type: InsuranceType;
  policy_number: string;
  subject: string;
  description: string;
}
