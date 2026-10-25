export type InsuranceType = 'health' | 'general' | 'both';
export type ComplaintStatus =
  | 'new'
  | 'assigned'
  | 'in_review'
  | 'resolved'
  | 'reopened'
  | 'transferred';
export type ComplaintPriority = 'low' | 'normal' | 'high' | 'urgent';
export type CommunicationChannel = 'portal' | 'email' | 'whatsapp' | 'phone' | 'ai_voice';
export type OwnershipLevel = 'own' | 'team' | 'all';
export type PermissionEffect = 'allow' | 'deny';

export const PERMISSIONS = [
  'complaints.view',
  'complaints.create',
  'complaints.edit',
  'complaints.assign',
  'complaints.reassign',
  'complaints.change_status',
  'complaints.reply_public',
  'complaints.add_internal_note',
  'complaints.merge',
  'complaints.escalate',
  'complaints.delete',
  'complaints.export',
  'complaints.view_attachments',
  'complaints.view_full_national_id',
  'analytics.view',
  'analytics.export',
  'users.view',
  'users.create',
  'users.edit',
  'users.deactivate',
  'users.manage_access',
  'roles.manage',
  'teams.manage',
  'companies.manage',
  'categories.manage',
  'sla.manage',
  'templates.manage',
  'integrations.manage',
  'integrations.send_email',
  'integrations.send_whatsapp',
  'integrations.ai_call',
  'reports.manage',
  'audit.view',
  'settings.manage',
  'storage.manage',
  'crm.view',
  'crm.manage',
  'crm.convert',
  'policies.view',
  'policies.manage',
  'issuing.view',
  'issuing.manage',
  'imports.manage',
  'payments.manage',
] as const;

export type Permission = (typeof PERMISSIONS)[number];

export interface StaffAccess {
  id: string;
  email: string;
  full_name: string;
  status: 'active' | 'invited' | 'disabled';
  token_version: number;
  two_factor_enabled: boolean;
  two_factor_required: boolean;
  roles: string[];
  permissions: Permission[];
  overrides: Record<string, PermissionEffect>;
  all_companies: boolean;
  company_ids: string[];
  insurance_types: InsuranceType[];
  ownership: OwnershipLevel;
  team_ids: string[];
}

export interface StaffMeResponse {
  kind: 'staff';
  access: StaffAccess;
}

export interface CustomerMeResponse {
  kind: 'customer';
  id: string;
  full_name: string;
}

export type MeResponse = StaffMeResponse | CustomerMeResponse;

export interface CompanyOption {
  id: string;
  name_en: string;
  name_ar: string;
  insurance_types: InsuranceType[];
}

export interface AgentOption {
  id: string;
  full_name: string;
}

export interface ComplaintFilterOptions {
  companies: CompanyOption[];
  agents: AgentOption[];
  statuses: ComplaintStatus[];
  priorities: ComplaintPriority[];
  channels: CommunicationChannel[];
  insurance_types: InsuranceType[];
}

export interface Complaint {
  id: string;
  ticket_no: string;
  status: ComplaintStatus;
  company_id: string;
  insurance_type: InsuranceType;
  subject: string;
  priority: ComplaintPriority;
  category_id: string | null;
  channel: CommunicationChannel;
  assigned_agent_id: string | null;
  sla_due_at: string | null;
  response_due_at?: string | null;
  sla_breached_at: string | null;
  merged_into_id: string | null;
  created_at: string;
  updated_at: string;
  description?: string;
  policy_number?: string;
  customer_name?: string;
  national_id_masked?: string;
  company_name?: string;
  resolved_at?: string | null;
  reopened_count?: number;
}

export interface ComplaintListResponse {
  items: Complaint[];
  total: number;
  page: number;
  page_size: number;
}

export interface TimelineEvent {
  id: string;
  event_type: string;
  from_status: ComplaintStatus | null;
  to_status: ComplaintStatus | null;
  occurred_at: string;
  metadata?: Record<string, unknown>;
}

export interface TimelineMessage {
  id: string;
  body: string;
  visibility: 'public' | 'internal';
  direction: 'inbound' | 'outbound';
  channel?: CommunicationChannel;
  created_at: string;
}

export interface TicketTimeline {
  events: TimelineEvent[];
  messages: TimelineMessage[];
  page: number;
}

/** A phone call as the unified inbox reports it. */
export interface InboxCall {
  id: string;
  kind: 'call';
  direction: 'inbound' | 'outbound';
  status: string;
  outcome: string | null;
  transcript: string | null;
  consent_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  /** The audio lives behind a separately audited route; this only says it exists. */
  has_recording: boolean;
  duration_seconds: number | null;
}

export interface UnifiedInbox {
  ticket_no: string;
  items: Array<{ kind: string } & Partial<InboxCall>>;
}

export interface AnalyticsKpis {
  total: number;
  new: number;
  assigned: number;
  in_review: number;
  resolved: number;
  reopened: number;
  transferred: number;
  avg_resolution_hours: number | null;
  sla_breaches: number;
  reopen_rate: number;
  csat_avg: number | null;
}

export interface AnalyticsOverview {
  kpis: AnalyticsKpis;
  time_series: Array<{ date: string; arrived: number }>;
  by_status: Array<{ status: ComplaintStatus; count: number }>;
  by_company: Array<{ company_id: string; name: string; count: number }>;
  by_type: Array<{ insurance_type: InsuranceType; count: number }>;
  by_agent: Array<{ agent_id: string | null; name?: string | null; count: number }>;
  resolution_hours: Record<string, number>;
  heatmap: Array<{ weekday: number; hour: number; count: number }>;
  top_issues: Array<{ category: string; count: number }>;
  company_scorecards: Array<{
    company_id: string;
    name: string;
    total: number;
    avg_resolution_hours: number | null;
    sla_breaches: number;
    csat_avg: number | null;
  }>;
  window: { date_from: string; date_to: string };
}

export interface StaffDepartment {
  id: string;
  name: string;
  description: string;
  is_active: boolean;
  employee_count: number;
  created_at?: string;
}

export interface StaffEmployeeProfile {
  id: string;
  email: string;
  full_name: string;
  status: string;
  department_id: string;
  department_name: string;
  department_description?: string;
  department_is_active?: boolean;
  avatar_url: string | null;
  created_at?: string;
  last_login_at?: string | null;
  two_factor_enabled?: boolean;
  two_factor_required?: boolean;
  roles?: Array<{ id: string; name: string; description: string; is_system: boolean }>;
  permissions?: string[];
  profile_completed_at: string | null;
  national_id_masked: string | null;
  personal: null | {
    full_name: string;
    national_id: string;
    mobile: string;
    address: string;
    city: string;
    job_title: string;
    emergency_phone: string | null;
    date_of_birth: string;
  };
}

export interface StaffDirectoryEntry {
  id: string;
  email: string;
  full_name: string;
  status: 'active' | 'invited' | 'disabled';
  two_factor_enabled: boolean;
  two_factor_required: boolean;
  last_login_at?: string | null;
  department_id?: string;
  department_name?: string;
  avatar_url?: string | null;
  profile_completed_at?: string | null;
}

export interface Role {
  id: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: Permission[];
  users_count?: number;
}

export interface AuditEvent {
  id: string;
  created_at: string;
  actor_staff_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  complaint_id: string | null;
}

export interface AccessChange {
  id: string;
  actor_staff_id: string;
  target_user_id: string;
  change_type: string;
  before_state: Record<string, unknown>;
  after_state: Record<string, unknown>;
  created_at: string;
}

export interface SavedView {
  id: string;
  name: string;
  criteria: Record<string, unknown>;
}

export interface Attachment {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  scan_status: 'pending' | 'clean' | 'infected' | 'error';
}

export interface NotificationItem {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  /** Complaint this notice belongs to (null for access-change notices). */
  complaint_id: string | null;
  created_at: string;
  read_at: string | null;
}

export interface DateRange {
  from: string;
  to: string;
  preset: '7d' | '30d' | '90d' | 'year' | 'custom';
}

export type AppRoute =
  | { page: 'overview' }
  | { page: 'admin' }
  | { page: 'complaints' }
  | { page: 'ticket'; id: string }
  | { page: 'users' }
  | { page: 'customers' }
  | { page: 'roles' }
  | { page: 'channels' }
  | { page: 'operations' }
  | { page: 'audit' }
  | { page: 'crm' }
  | { page: 'crm-module'; tab: 'dashboard' | 'leads' | 'import' | 'history' }
  | { page: 'storage' };
