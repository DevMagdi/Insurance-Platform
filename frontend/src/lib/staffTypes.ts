import type { ComplaintStatus, InsuranceType } from './types';

export interface StaffAccess {
  id: string;
  email: string;
  full_name: string;
  status: string;
  two_factor_enabled: boolean;
  two_factor_required: boolean;
  roles: string[];
  permissions: string[];
  overrides: Record<string, 'allow' | 'deny'>;
  all_companies: boolean;
  company_ids: string[];
  insurance_types: InsuranceType[];
  ownership: 'all' | 'team' | 'own' | null;
  team_ids: string[];
}

export interface StaffMe {
  kind: 'staff';
  access: StaffAccess;
}

export interface StaffComplaintFilters {
  status?: ComplaintStatus;
  company_id?: string;
  insurance_type?: InsuranceType;
  assigned_agent_id?: string;
  priority?: 'low' | 'normal' | 'high' | 'urgent';
  category_id?: string;
  channel?: string;
  date_from?: string;
  date_to?: string;
  ticket_no?: string;
  search?: string;
  sort_by?: 'created_at' | 'updated_at' | 'priority' | 'status';
  sort_dir?: 'asc' | 'desc';
  page?: number;
  page_size?: number;
}

export interface StaffFilterOptions {
  companies: { id: string; name_en: string; name_ar: string; insurance_types: InsuranceType[] }[];
  statuses: ComplaintStatus[];
  priorities: string[];
  channels: string[];
  insurance_types: InsuranceType[];
}

export interface SavedView {
  id: string;
  name: string;
  criteria: StaffComplaintFilters;
}

export interface CannedReply {
  id: string;
  title: string;
  body_en: string;
  body_ar: string;
  company_id: string | null;
  is_active: boolean;
}

export interface AiInsight {
  id: string;
  kind: 'classification' | 'sentiment' | 'summary' | 'reply';
  payload: Record<string, unknown>;
  confidence: number | null;
  provider_model: string | null;
  review_status: 'suggested' | 'accepted' | 'rejected';
  created_at: string;
  job_id?: string;
  sent_message_id?: string | null;
}

export interface IntegrationJobQueued {
  job_id: string;
  state: string;
  created: boolean;
}

export interface IntegrationJob {
  id: string;
  kind: string;
  state: string;
  attempt_count: number;
  provider_message_id: string | null;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface StaffNotification {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
  read_at: string | null;
}

export interface StaffUser {
  id: string;
  email: string;
  full_name: string;
  status: string;
  two_factor_enabled: boolean;
  two_factor_required: boolean;
}

export interface StaffRole {
  id: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

export interface AnalyticsOverview {
  kpis: Record<string, number | null>;
  time_series: { date: string; arrived: number }[];
  by_status: { status: string; count: number }[];
  by_company: { company_id: string; name: string; count: number }[];
  by_type: { insurance_type: string; count: number }[];
  by_agent: { agent_id: string | null; count: number }[];
  resolution_hours: Record<string, number>;
  heatmap: { weekday: number; hour: number; count: number }[];
  top_issues: { category: string; count: number }[];
  company_scorecards: Record<string, unknown>[];
  window: { date_from: string; date_to: string };
}

export interface ExportJob {
  id: string;
  kind: string;
  status: string;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
}

export function hasPermission(access: StaffAccess | null | undefined, permission: string): boolean {
  return Boolean(access?.permissions.includes(permission));
}

export interface AccessSpecBody {
  role_ids: string[];
  allow: string[];
  deny: string[];
  all_companies: boolean;
  company_ids: string[];
  insurance_types: InsuranceType[];
  ownership: 'all' | 'team' | 'own';
  team_ids: string[];
}

/** Convert the read-only access snapshot into the strict PUT /access body. */
export function accessSpecFromSnapshot(snapshot: Record<string, unknown>): AccessSpecBody {
  const overrides =
    snapshot.overrides && typeof snapshot.overrides === 'object'
      ? (snapshot.overrides as Record<string, unknown>)
      : {};
  const strings = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  return {
    role_ids: strings(snapshot.roles),
    allow: Object.entries(overrides)
      .filter(([, effect]) => effect === 'allow')
      .map(([code]) => code),
    deny: Object.entries(overrides)
      .filter(([, effect]) => effect === 'deny')
      .map(([code]) => code),
    all_companies: snapshot.all_companies === true,
    company_ids: strings(snapshot.company_ids),
    insurance_types: strings(snapshot.insurance_types) as InsuranceType[],
    ownership:
      snapshot.ownership === 'team' || snapshot.ownership === 'own' ? snapshot.ownership : 'all',
    team_ids: strings(snapshot.team_ids),
  };
}

const CATALOG_FIELDS: Record<string, string[]> = {
  companies: ['slug', 'name_en', 'name_ar', 'contact_emails', 'supported_types', 'is_active'],
  categories: ['company_id', 'parent_id', 'name_en', 'name_ar', 'is_active'],
  'sla-rules': [
    'company_id',
    'insurance_type',
    'priority',
    'response_minutes',
    'resolution_minutes',
    'business_hours',
    'is_active',
  ],
  templates: ['company_id', 'title', 'body_en', 'body_ar', 'is_active'],
};

/** Remove read-only fields (id/timestamps) before sending a strict catalog spec. */
export function catalogSpec(
  kind: 'companies' | 'categories' | 'sla-rules' | 'templates',
  record: Record<string, unknown>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const field of CATALOG_FIELDS[kind]) {
    if (record[field] !== undefined) result[field] = record[field];
  }
  if (kind === 'sla-rules' && result.business_hours === undefined) result.business_hours = {};
  return result;
}

export function staffComplaintsQuery(filters: StaffComplaintFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}
