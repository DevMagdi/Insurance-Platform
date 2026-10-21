import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { ErrorBoundary } from '../../components/ErrorBoundary';
import { IntegrationsSettings } from './IntegrationsSettings';
import { BrandSettings } from './BrandSettings';
import { formatDateTime, formatNumber } from '../../utils/date';
import { COMPANY_SLUG_PATTERN_SOURCE, duplicateEmailList, invalidEmailList, isValidCompanySlug, parseEmailList } from '../../lib/validation';
import type { CompanyOption, DateRange, InsuranceType, StaffAccess } from '../../types';

interface CompanyRecord { id: string; slug: string; name_en: string; name_ar: string; contact_emails: string[]; supported_types: InsuranceType[]; is_active: boolean; }
interface CategoryRecord { id: string; company_id: string | null; parent_id: string | null; name_en: string; name_ar: string; is_active: boolean; }
interface SlaRecord { id: string; company_id: string | null; insurance_type: InsuranceType | null; priority: string | null; response_minutes: number | null; resolution_minutes: number | null; is_active: boolean; }
interface TemplateRecord { id: string; company_id: string | null; title: string; body_en: string; body_ar: string; is_active: boolean; }
interface TeamRecord { id: string; name: string; lead_staff_id: string | null; member_ids: string[]; }
interface AssignmentRecord { id: string; company_id: string | null; insurance_type: InsuranceType | null; category_id: string | null; priority: string | null; target_team_id: string | null; target_agent_id: string | null; strategy: 'round_robin' | 'least_workload' | 'by_company'; sort_order: number | null; is_active: boolean; }
interface ReportRecord { id: string; name: string; filters: { window_days?: number; company_id?: string | null; insurance_type?: InsuranceType | null }; cron_expression: string; time_zone: string; recipient_emails: string[]; format: 'csv' | 'xlsx'; is_active: boolean; next_run_at: string | null; last_run_at: string | null; last_state: string | null; created_at: string; }
type AdminRecord = CompanyRecord | CategoryRecord | SlaRecord | TemplateRecord | TeamRecord | AssignmentRecord | ReportRecord;
type OperationsTab = 'companies' | 'categories' | 'sla' | 'templates' | 'teams' | 'assignment' | 'integrations' | 'reports';
interface AutomationOptions { companies: Array<{ id: string; name_en: string; name_ar: string }>; categories: Array<{ id: string; company_id: string | null; name_en: string }>; teams: Array<{ id: string; name: string }>; agents: Array<{ id: string; full_name: string }>; }

interface TabSpec { id: OperationsTab; label: string; permission: string; icon: 'building' | 'filter' | 'clock' | 'message' | 'users' | 'refresh' | 'globe' | 'calendar'; }
const TAB_SPECS: TabSpec[] = [
  { id: 'companies', label: 'شركات التأمين', permission: 'companies.manage', icon: 'building' },
  { id: 'categories', label: 'التصنيفات', permission: 'categories.manage', icon: 'filter' },
  { id: 'sla', label: 'قواعد SLA', permission: 'sla.manage', icon: 'clock' },
  { id: 'templates', label: 'الردود الجاهزة', permission: 'templates.manage', icon: 'message' },
  { id: 'teams', label: 'الفرق', permission: 'teams.manage', icon: 'users' },
  { id: 'assignment', label: 'التوزيع التلقائي', permission: 'settings.manage', icon: 'refresh' },
  { id: 'integrations', label: 'التكاملات', permission: 'integrations.manage', icon: 'globe' },
  { id: 'reports', label: 'التقارير المجدولة', permission: 'reports.manage', icon: 'calendar' },
];
const INSURANCE_TYPES: Array<{ id: InsuranceType; label: string }> = [{ id: 'health', label: 'طبي' }, { id: 'general', label: 'عام' }, { id: 'both', label: 'مشترك' }];
const PRIORITIES = [{ id: 'low', label: 'منخفضة' }, { id: 'normal', label: 'عادية' }, { id: 'high', label: 'مرتفعة' }, { id: 'urgent', label: 'عاجلة' }];

function hasGlobalAdminScope(access: StaffAccess): boolean {
  return access.all_companies && access.company_ids.length === 0
    && access.insurance_types.length === 0 && access.ownership === 'all';
}

function canOpenTab(access: StaffAccess, tab: TabSpec): boolean {
  if (!hasPermission(access, tab.permission)) return false;
  if (tab.id === 'teams') return hasGlobalAdminScope(access) && hasPermission(access, 'users.view');
  if (tab.id === 'assignment') return hasGlobalAdminScope(access) && hasPermission(access, 'complaints.assign');
  if (tab.id === 'reports') return hasPermission(access, 'analytics.view') && hasPermission(access, 'analytics.export');
  return true;
}

interface OperationsPageProps { access: StaffAccess; range: DateRange; inScopeCompanies: CompanyOption[]; }

function recordValue(record: AdminRecord | undefined, key: string): unknown {
  if (!record) return undefined;
  return (record as unknown as Record<string, unknown>)[key];
}
function recordId(record: AdminRecord | undefined): string | undefined {
  const value = recordValue(record, 'id');
  return typeof value === 'string' ? value : undefined;
}
function typeLabel(type: InsuranceType | null | undefined): string { return type ? INSURANCE_TYPES.find((item) => item.id === type)?.label ?? type : 'كل الأنواع'; }

/*
 * Normalization layer: the table only ever renders rows produced here.
 *
 * Every payload is `unknown` until it passes through a normalizer. The API can
 * answer with a paginated envelope, a partial record (an old row, a worker
 * write, a proxy that dropped a field) or — the crash behind
 * `Cannot read properties of undefined (reading 'join')` — a response that
 * belongs to the tab the operator has already left. Casting the payload
 * straight into a record type (`visibleRows as ReportRecord[]`) turned any of
 * those into a TypeError that blanked the whole page, so the casts are gone and
 * each field is coerced with a safe default instead.
 */
function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function asText(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return fallback;
}
function asNullableText(value: unknown): string | null {
  const text = asText(value).trim();
  return text ? text : null;
}
function asTextList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => asText(item).trim()).filter((item) => item !== '') : [];
}
function asOptionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
function asBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}
function asInsuranceType(value: unknown): InsuranceType | null {
  const type = asText(value);
  return type === 'health' || type === 'general' || type === 'both' ? type : null;
}
function asStrategy(value: unknown): AssignmentRecord['strategy'] {
  const strategy = asText(value);
  return strategy === 'round_robin' || strategy === 'by_company' ? strategy : 'least_workload';
}
/** A missing field renders as a dash, never as `undefined` or an empty cell. */
function display(value: string | null | undefined, fallback = '—'): string {
  const text = (value ?? '').trim();
  return text ? text : fallback;
}
function preview(value: string | null | undefined, length = 56): string {
  const text = (value ?? '').trim();
  if (!text) return '—';
  return text.length > length ? `${text.slice(0, length)}…` : text;
}
function initialLetter(value: string | null | undefined): string {
  const text = (value ?? '').trim();
  return text ? text.slice(0, 1) : '·';
}
/** Accepts a bare array or a paginated envelope; anything else means "no rows". */
function unwrapList(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  const envelope = asRecord(payload);
  for (const key of ['items', 'data', 'results', 'rows']) {
    const value = envelope[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}
function safeJson(value: unknown): string {
  try { return JSON.stringify(value) ?? ''; } catch { return ''; }
}

function normalizeCompany(row: unknown): CompanyRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return {
    id,
    slug: asText(raw.slug),
    name_en: asText(raw.name_en),
    name_ar: asText(raw.name_ar),
    contact_emails: asTextList(raw.contact_emails),
    supported_types: asTextList(raw.supported_types) as InsuranceType[],
    is_active: asBoolean(raw.is_active, true),
  };
}
function normalizeCategory(row: unknown): CategoryRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return {
    id,
    company_id: asNullableText(raw.company_id),
    parent_id: asNullableText(raw.parent_id),
    name_en: asText(raw.name_en),
    name_ar: asText(raw.name_ar),
    is_active: asBoolean(raw.is_active, true),
  };
}
function normalizeSla(row: unknown): SlaRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return {
    id,
    company_id: asNullableText(raw.company_id),
    insurance_type: asInsuranceType(raw.insurance_type),
    priority: asNullableText(raw.priority),
    response_minutes: asOptionalNumber(raw.response_minutes),
    resolution_minutes: asOptionalNumber(raw.resolution_minutes),
    is_active: asBoolean(raw.is_active, true),
  };
}
function normalizeTemplate(row: unknown): TemplateRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return {
    id,
    company_id: asNullableText(raw.company_id),
    title: asText(raw.title),
    body_en: asText(raw.body_en),
    body_ar: asText(raw.body_ar),
    is_active: asBoolean(raw.is_active, true),
  };
}
function normalizeTeam(row: unknown): TeamRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return { id, name: asText(raw.name), lead_staff_id: asNullableText(raw.lead_staff_id), member_ids: asTextList(raw.member_ids) };
}
function normalizeAssignment(row: unknown): AssignmentRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  return {
    id,
    company_id: asNullableText(raw.company_id),
    insurance_type: asInsuranceType(raw.insurance_type),
    category_id: asNullableText(raw.category_id),
    priority: asNullableText(raw.priority),
    target_team_id: asNullableText(raw.target_team_id),
    target_agent_id: asNullableText(raw.target_agent_id),
    strategy: asStrategy(raw.strategy),
    sort_order: asOptionalNumber(raw.sort_order),
    is_active: asBoolean(raw.is_active, true),
  };
}
function normalizeReport(row: unknown): ReportRecord | null {
  const raw = asRecord(row);
  const id = asText(raw.id).trim();
  if (!id) return null;
  const filters = asRecord(raw.filters);
  const windowDays = asOptionalNumber(filters.window_days);
  return {
    id,
    name: asText(raw.name),
    // `filters` is always an object so the render can read `window_days`
    // without the optional chaining that only moved the crash to `.join`.
    filters: {
      window_days: windowDays === null ? undefined : windowDays,
      company_id: asNullableText(filters.company_id),
      insurance_type: asInsuranceType(filters.insurance_type),
    },
    cron_expression: asText(raw.cron_expression),
    time_zone: asText(raw.time_zone, 'Africa/Cairo'),
    recipient_emails: asTextList(raw.recipient_emails),
    format: raw.format === 'xlsx' ? 'xlsx' : 'csv',
    is_active: asBoolean(raw.is_active, true),
    next_run_at: asNullableText(raw.next_run_at),
    last_run_at: asNullableText(raw.last_run_at),
    last_state: asNullableText(raw.last_state),
    created_at: asText(raw.created_at),
  };
}

/** The rows of the active tab plus how many unusable ones were dropped. */
interface OperationsTable {
  companies: CompanyRecord[];
  categories: CategoryRecord[];
  sla: SlaRecord[];
  templates: TemplateRecord[];
  teams: TeamRecord[];
  assignment: AssignmentRecord[];
  reports: ReportRecord[];
  shown: number;
  skipped: number;
}
function normalizeList<T>(rows: unknown[], normalize: (row: unknown) => T | null): { values: T[]; skipped: number } {
  const values: T[] = [];
  let skipped = 0;
  for (const row of rows) {
    const value = normalize(row);
    if (value) values.push(value);
    else skipped += 1;
  }
  return { values, skipped };
}
function normalizeTable(tab: OperationsTab, rows: unknown[]): OperationsTable {
  const companies = tab === 'companies' ? normalizeList(rows, normalizeCompany) : null;
  const categories = tab === 'categories' ? normalizeList(rows, normalizeCategory) : null;
  const sla = tab === 'sla' ? normalizeList(rows, normalizeSla) : null;
  const templates = tab === 'templates' ? normalizeList(rows, normalizeTemplate) : null;
  const teams = tab === 'teams' ? normalizeList(rows, normalizeTeam) : null;
  const assignment = tab === 'assignment' ? normalizeList(rows, normalizeAssignment) : null;
  const reports = tab === 'reports' ? normalizeList(rows, normalizeReport) : null;
  const active = companies ?? categories ?? sla ?? templates ?? teams ?? assignment ?? reports;
  return {
    companies: companies?.values ?? [],
    categories: categories?.values ?? [],
    sla: sla?.values ?? [],
    templates: templates?.values ?? [],
    teams: teams?.values ?? [],
    assignment: assignment?.values ?? [],
    reports: reports?.values ?? [],
    shown: active?.values.length ?? 0,
    skipped: active?.skipped ?? 0,
  };
}

const TAB_ENDPOINTS: Partial<Record<OperationsTab, string>> = {
  companies: '/api/staff/admin/companies',
  categories: '/api/staff/admin/categories',
  sla: '/api/staff/admin/sla-rules',
  templates: '/api/staff/admin/templates',
  teams: '/api/staff/admin/teams',
  assignment: '/api/staff/admin/assignment-rules',
  reports: '/api/staff/reports/schedules',
};
/** Only `id` and `full_name` of a staff entry are used here. */
interface AgentOption { id: string; full_name: string; }
function normalizeAgents(payload: unknown): AgentOption[] {
  return unwrapList(payload)
    .map((item) => { const raw = asRecord(item); return { id: asText(raw.id).trim(), full_name: asText(raw.full_name) }; })
    .filter((agent) => agent.id !== '');
}
/** Builds one select-option list, dropping entries the API left without an id. */
function optionList<T extends { id: string }>(value: unknown, build: (raw: Record<string, unknown>) => T): T[] {
  return unwrapList(value).map((item) => build(asRecord(item))).filter((entry) => entry.id !== '');
}
function normalizeAutomationOptions(payload: unknown): AutomationOptions {
  const raw = asRecord(payload);
  return {
    companies: optionList(raw.companies, (item) => ({ id: asText(item.id).trim(), name_en: asText(item.name_en), name_ar: asText(item.name_ar) })),
    categories: optionList(raw.categories, (item) => ({ id: asText(item.id).trim(), company_id: asNullableText(item.company_id), name_en: asText(item.name_en) })),
    teams: optionList(raw.teams, (item) => ({ id: asText(item.id).trim(), name: asText(item.name) })),
    agents: optionList(raw.agents, (item) => ({ id: asText(item.id).trim(), full_name: asText(item.full_name) })),
  };
}
/** `—` instead of a dangling "دقيقة" when the API omits an SLA duration. */
function minutes(value: number | null): string {
  return value === null ? '—' : `${formatNumber(value)} دقيقة`;
}
function fetchTabRows(tab: OperationsTab): Promise<unknown> {
  const endpoint = TAB_ENDPOINTS[tab];
  return endpoint ? api<unknown>(endpoint) : Promise.resolve([]);
}

function OperationEditor({
  tab, record, options, inScopeCompanies, agents, onClose, onSaved,
}: {
  tab: OperationsTab;
  record?: AdminRecord;
  options: AutomationOptions | null;
  inScopeCompanies: CompanyOption[];
  agents: AgentOption[];
  onClose: () => void;
  onSaved: (data: Record<string, unknown>, id?: string) => Promise<void>;
}) {
  const initial = record ? record as unknown as Record<string, unknown> : {};
  const initialFilters = initial.filters && typeof initial.filters === 'object'
    ? initial.filters as Record<string, unknown>
    : {};
  const [name, setName] = useState(String(initial.name ?? initial.name_en ?? initial.title ?? ''));
  const [slug, setSlug] = useState(String(initial.slug ?? ''));
  const [nameEn, setNameEn] = useState(String(initial.name_en ?? initial.title ?? ''));
  const [nameAr, setNameAr] = useState(String(initial.name_ar ?? ''));
  const [emails, setEmails] = useState(Array.isArray(initial.contact_emails) ? initial.contact_emails.join(', ') : '');
  const [companyId, setCompanyId] = useState(String(initial.company_id ?? initialFilters.company_id ?? ''));
  const [parentId, setParentId] = useState(String(initial.parent_id ?? initialFilters.category_id ?? ''));
  const [insuranceType, setInsuranceType] = useState(String(initial.insurance_type ?? initialFilters.insurance_type ?? ''));
  const [priority, setPriority] = useState(String(initial.priority ?? ''));
  const [responseMinutes, setResponseMinutes] = useState(String(initial.response_minutes ?? 60));
  const [resolutionMinutes, setResolutionMinutes] = useState(String(initial.resolution_minutes ?? 1440));
  const [bodyEn, setBodyEn] = useState(String(initial.body_en ?? ''));
  const [bodyAr, setBodyAr] = useState(String(initial.body_ar ?? ''));
  const [active, setActive] = useState(tab === 'integrations' ? Boolean(initial.enabled) : typeof initial.is_active === 'boolean' ? initial.is_active : true);
  const [supportedTypes, setSupportedTypes] = useState<InsuranceType[]>(Array.isArray(initial.supported_types) ? initial.supported_types as InsuranceType[] : ['health', 'general', 'both']);
  const [memberIds, setMemberIds] = useState<string[]>(Array.isArray(initial.member_ids) ? initial.member_ids as string[] : []);
  const [leadId, setLeadId] = useState(String(initial.lead_staff_id ?? ''));
  const [targetTeamId, setTargetTeamId] = useState(String(initial.target_team_id ?? ''));
  const [targetAgentId, setTargetAgentId] = useState(String(initial.target_agent_id ?? ''));
  const [strategy, setStrategy] = useState(String(initial.strategy ?? 'least_workload'));
  const [sortOrder, setSortOrder] = useState(String(initial.sort_order ?? 100));
  const [recipients, setRecipients] = useState(Array.isArray(initial.recipient_emails) ? initial.recipient_emails.join(', ') : '');
  const [cron, setCron] = useState(String(initial.cron_expression ?? '0 8 * * 1'));
  const [timeZone, setTimeZone] = useState(String(initial.time_zone ?? 'Africa/Cairo'));
  const [windowDays, setWindowDays] = useState(String(initialFilters.window_days !== undefined ? initialFilters.window_days : 7));
  const [reportFormat, setReportFormat] = useState<'csv' | 'xlsx'>(initial.format === 'xlsx' ? 'xlsx' : 'csv');
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);
  const companyOptions = options?.companies ?? inScopeCompanies.map((company) => ({ id: company.id, name_en: company.name_en, name_ar: company.name_ar }));
  const availableAgents = options?.agents ?? agents;
  const availableTeams = options?.teams ?? [];
  const availableCategories = options?.categories ?? [];
  const editing = Boolean(recordId(record));

  const toggleType = (type: InsuranceType) => setSupportedTypes((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type]);
  // The team lead is always a member of the team: the API rejects a team whose
  // leader is not part of the member list.
  const toggleMember = (id: string) => {
    if (id === leadId) return;
    setMemberIds((current) => current.includes(id) ? current.filter((member) => member !== id) : [...current, id]);
  };
  const requiredText = (value: string, label: string) => { if (!value.trim()) throw new Error(`أدخل ${label}.`); return value.trim(); };
  const requiredEmailList = (value: string, label: string, requireOne: boolean) => {
    const invalid = invalidEmailList(value);
    if (invalid.length) throw new Error(`عنوان البريد غير صالح في ${label}: ${invalid[0]}`);
    const duplicates = duplicateEmailList(value);
    if (duplicates.length) throw new Error(`عنوان البريد مكرر في ${label}: ${duplicates[0]}`);
    const addresses = parseEmailList(value);
    if (requireOne && !addresses.length) throw new Error(`أدخل عنوان بريد واحداً على الأقل في ${label}.`);
    if (addresses.length > 10) throw new Error(`الحد الأقصى 10 عناوين في ${label}.`);
    return addresses;
  };
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setSaving(true); setFormError('');
    try {
      let data: Record<string, unknown>;
      if (tab === 'companies') {
        if (!supportedTypes.length) throw new Error('اختر نوع تأمين واحداً على الأقل.');
        // Same rule as the input's `pattern` and the backend `CompanySpec.slug`;
        // stated in Arabic so a rejected save explains itself.
        if (!isValidCompanySlug(slug.trim())) throw new Error('المعرّف المختصر يجب أن يبدأ بحرف إنجليزي صغير، ويتكون من أحرف صغيرة وأرقام وشرطات فقط (حرفان على الأقل).');
        const parsedEmails = requiredEmailList(emails, 'عناوين التواصل', false);
        data = { slug: requiredText(slug, 'المعرّف المختصر'), name_en: requiredText(nameEn, 'الاسم بالإنجليزية'), name_ar: requiredText(nameAr, 'الاسم بالعربية'), contact_emails: parsedEmails, supported_types: supportedTypes, is_active: active };
      } else if (tab === 'categories') {
        data = { company_id: companyId || null, parent_id: parentId || null, name_en: requiredText(nameEn, 'الاسم بالإنجليزية'), name_ar: requiredText(nameAr, 'الاسم بالعربية'), is_active: active };
      } else if (tab === 'sla') {
        const response = Number(responseMinutes);
        const resolution = Number(resolutionMinutes);
        if (!Number.isFinite(response) || response < 1) throw new Error('زمن الاستجابة يجب أن يكون رقماً لا يقل عن دقيقة.');
        if (!Number.isFinite(resolution) || resolution < 1) throw new Error('زمن الحل يجب أن يكون رقماً لا يقل عن دقيقة.');
        if (resolution < response) throw new Error('زمن الحل يجب ألا يقل عن زمن الاستجابة.');
        data = { company_id: companyId || null, insurance_type: insuranceType || null, priority: priority || null, response_minutes: response, resolution_minutes: resolution, business_hours: null, is_active: active };
      } else if (tab === 'templates') {
        data = { company_id: companyId || null, title: requiredText(name, 'عنوان الرد'), body_en: requiredText(bodyEn, 'نص الرد بالإنجليزية'), body_ar: requiredText(bodyAr, 'نص الرد بالعربية'), is_active: active };
      } else if (tab === 'teams') {
        // The leader is always a member; the API rejects anything else.
        const members = leadId ? [...new Set([...memberIds, leadId])] : [...new Set(memberIds)];
        data = { name: requiredText(name, 'اسم الفريق'), lead_staff_id: leadId || null, member_ids: members };
      } else if (tab === 'assignment') {
        // The API (and the database) refuse a rule with no destination: it
        // would match every ticket and route it nowhere.
        if (!targetTeamId && !targetAgentId) throw new Error('اختر فريقاً أو موظفاً محدداً كهدف للقاعدة.');
        data = { company_id: companyId || null, insurance_type: insuranceType || null, category_id: parentId || null, priority: priority || null, target_team_id: targetTeamId || null, target_agent_id: targetAgentId || null, strategy, sort_order: Number(sortOrder), is_active: active };
      } else {
        data = {
          name: requiredText(name, 'اسم التقرير'),
          filters: {
            window_days: Number(windowDays),
            company_id: companyId || null,
            insurance_type: insuranceType || null,
          },
          cron_expression: requiredText(cron, 'جدول التكرار'),
          time_zone: requiredText(timeZone, 'المنطقة الزمنية'),
          recipient_emails: requiredEmailList(recipients, 'المستلمين', true),
          format: reportFormat,
          is_active: active,
        };
      }
      await onSaved(data, recordId(record));
    } catch (cause) { setFormError(cause instanceof Error ? cause.message : 'تعذر حفظ الإعداد.'); }
    finally { setSaving(false); }
  };

  const title = editing ? 'تعديل الإعداد' : 'إضافة إعداد';
  const commonCompanySelect = <label className="field-label">شركة التأمين<select value={companyId} onChange={(event) => setCompanyId(event.target.value)}><option value="">كل الشركات / إعداد عام</option>{companyOptions.map((company) => <option key={company.id} value={company.id}>{company.name_ar || company.name_en}</option>)}</select></label>;
  const activeToggle = tab !== 'teams' && <label className="switch-row"><span><strong>{tab === 'reports' ? 'تفعيل الجدولة' : 'مفعّل'}</strong><small>{tab === 'reports' ? 'يعيد العامل التحقق من صلاحيات المالك عند كل تشغيل.' : 'إظهار هذا العنصر في مساحة العمل.'}</small></span><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} /><i /></label>;

  return <Modal title={title} eyebrow="إعدادات مساحة العمل" onClose={onClose} wide><form onSubmit={(event) => { void save(event); }}><div className="operation-form-grid">
    {tab === 'companies' && <>
      <label className="field-label">المعرّف المختصر<input autoFocus required pattern={COMPANY_SLUG_PATTERN_SOURCE} value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="nile-assurance" dir="ltr" /></label>
      <label className="field-label">الاسم بالإنجليزية<input required value={nameEn} onChange={(event) => setNameEn(event.target.value)} /></label>
      <label className="field-label">الاسم بالعربية<input required value={nameAr} onChange={(event) => setNameAr(event.target.value)} /></label>
      <label className="field-label">عناوين البريد للتواصل<input value={emails} onChange={(event) => setEmails(event.target.value)} placeholder="claims@example.com, support@example.com" dir="ltr" /><small>عناوين مفصولة بفاصلة (اختياري، وبأحرف صغيرة).</small></label>
      <div className="field-label full-form-row"><span>أنواع التأمين المدعومة</span><div className="scope-type-grid">{INSURANCE_TYPES.map((type) => <label className="type-choice" key={type.id}><input type="checkbox" checked={supportedTypes.includes(type.id)} onChange={() => toggleType(type.id)} /><span>{type.label}</span></label>)}</div></div>
      {activeToggle}
    </>}
    {tab === 'categories' && <>{commonCompanySelect}<label className="field-label">تصنيف رئيسي (اختياري)<select value={parentId} onChange={(event) => setParentId(event.target.value)}><option value="">بدون</option>{availableCategories.map((category) => <option value={category.id} key={category.id}>{category.name_en}</option>)}</select></label><label className="field-label">الاسم بالإنجليزية<input autoFocus required value={nameEn} onChange={(event) => setNameEn(event.target.value)} /></label><label className="field-label">الاسم بالعربية<input required value={nameAr} onChange={(event) => setNameAr(event.target.value)} /></label>{activeToggle}</>}
    {tab === 'sla' && <>{commonCompanySelect}<label className="field-label">نوع التأمين<select value={insuranceType} onChange={(event) => setInsuranceType(event.target.value)}><option value="">كل الأنواع</option>{INSURANCE_TYPES.map((type) => <option key={type.id} value={type.id}>{type.label}</option>)}</select></label><label className="field-label">الأولوية<select value={priority} onChange={(event) => setPriority(event.target.value)}><option value="">كل الأولويات</option>{PRIORITIES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label className="field-label">زمن الاستجابة (دقيقة)<input type="number" min={1} max={525600} value={responseMinutes} onChange={(event) => setResponseMinutes(event.target.value)} /></label><label className="field-label">زمن الحل (دقيقة)<input type="number" min={Math.max(1, Number(responseMinutes) || 1)} max={525600} value={resolutionMinutes} onChange={(event) => setResolutionMinutes(event.target.value)} /><small>لا يقل عن زمن الاستجابة.</small></label><div className="inline-alert warning full-form-row">قواعد SLA تعمل بالوقت المستمر حالياً؛ تقاويم ساعات العمل غير مفعّلة.</div>{activeToggle}</>}
    {tab === 'templates' && <>{commonCompanySelect}<label className="field-label">عنوان الرد<input autoFocus required value={name} onChange={(event) => setName(event.target.value)} /></label><label className="field-label">نص الرد بالعربية<textarea rows={4} required value={bodyAr} onChange={(event) => setBodyAr(event.target.value)} /></label><label className="field-label">نص الرد بالإنجليزية<textarea rows={4} required value={bodyEn} onChange={(event) => setBodyEn(event.target.value)} /></label>{activeToggle}</>}
    {tab === 'teams' && <><label className="field-label">اسم الفريق<input autoFocus required minLength={2} maxLength={80} value={name} onChange={(event) => setName(event.target.value)} /></label><label className="field-label">قائد الفريق<select value={leadId} onChange={(event) => { setLeadId(event.target.value); if (event.target.value && !memberIds.includes(event.target.value)) setMemberIds((current) => [...current, event.target.value]); }}><option value="">بدون قائد</option>{availableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.full_name}</option>)}</select></label><div className="field-label full-form-row"><span>أعضاء الفريق</span><div className="member-picker">{availableAgents.map((agent) => <label className="check-row" key={agent.id}><input type="checkbox" checked={memberIds.includes(agent.id) || agent.id === leadId} disabled={agent.id === leadId} onChange={() => toggleMember(agent.id)} /><span>{agent.full_name}{agent.id === leadId && <small> (قائد الفريق)</small>}</span></label>)}{availableAgents.length === 0 && <small>لا توجد قائمة مستخدمين متاحة. اطلب صلاحية عرض المستخدمين.</small>}</div></div><div className="inline-alert warning full-form-row">تعديل عضوية الفريق يُبطل الجلسات الحالية للأعضاء لتطبيق نطاق الملكية فوراً.</div></>}
    {tab === 'assignment' && <>{commonCompanySelect}<label className="field-label">نوع التأمين<select value={insuranceType} onChange={(event) => setInsuranceType(event.target.value)}><option value="">كل الأنواع</option>{INSURANCE_TYPES.map((type) => <option key={type.id} value={type.id}>{type.label}</option>)}</select></label><label className="field-label">التصنيف<select value={parentId} onChange={(event) => setParentId(event.target.value)}><option value="">كل التصنيفات</option>{availableCategories.map((category) => <option key={category.id} value={category.id}>{category.name_en}</option>)}</select></label><label className="field-label">الأولوية<select value={priority} onChange={(event) => setPriority(event.target.value)}><option value="">كل الأولويات</option>{PRIORITIES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label className="field-label">الفريق المستهدف<select value={targetTeamId} onChange={(event) => setTargetTeamId(event.target.value)}><option value="">بدون فريق</option>{availableTeams.map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}</select></label><label className="field-label">موظف محدد (اختياري)<select value={targetAgentId} onChange={(event) => setTargetAgentId(event.target.value)}><option value="">اختيار من الفريق أو حسب الحمل</option>{availableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.full_name}</option>)}</select></label><div className="inline-alert warning full-form-row">القاعدة تحتاج هدفاً: اختر فريقاً أو موظفاً محدداً، وإلا لن يتم توزيع أي طلب.</div><label className="field-label">استراتيجية التوزيع<select value={strategy} onChange={(event) => setStrategy(event.target.value)}><option value="least_workload">الأقل في حجم العمل</option><option value="round_robin">بالتناوب</option><option value="by_company">حسب الشركة</option></select></label><label className="field-label">ترتيب القاعدة<input type="number" min={0} max={100000} value={sortOrder} onChange={(event) => setSortOrder(event.target.value)} /></label>{activeToggle}</>}

    {tab === 'reports' && <><label className="field-label">اسم التقرير<input autoFocus required value={name} onChange={(event) => setName(event.target.value)} /></label><label className="field-label">المستلمون<input required value={recipients} onChange={(event) => setRecipients(event.target.value)} placeholder="team@example.com" dir="ltr" /><small>عناوين مفصولة بفاصلة، وكل عنوان صالح وفريد.</small></label><label className="field-label">جدول Cron من خمسة حقول<input required value={cron} onChange={(event) => setCron(event.target.value)} placeholder="0 8 * * 1" dir="ltr" /></label><label className="field-label">المنطقة الزمنية<input required value={timeZone} onChange={(event) => setTimeZone(event.target.value)} dir="ltr" /></label><label className="field-label">نافذة البيانات<select value={windowDays} onChange={(event) => setWindowDays(event.target.value)}>{[7, 14, 30, 90, 180, 366].map((days) => <option key={days} value={days}>آخر {days} يومًا</option>)}</select></label><label className="field-label">صيغة التقرير<select value={reportFormat} onChange={(event) => setReportFormat(event.target.value as 'csv' | 'xlsx')}><option value="csv">CSV</option><option value="xlsx">Excel (XLSX)</option></select></label>{commonCompanySelect}<label className="field-label">نوع التأمين<select value={insuranceType} onChange={(event) => setInsuranceType(event.target.value)}><option value="">كل الأنواع</option>{INSURANCE_TYPES.map((type) => <option key={type.id} value={type.id}>{type.label}</option>)}</select></label>{activeToggle}<div className="inline-alert info full-form-row">يحسب الخادم موعد التشغيل من تعبير Cron والمنطقة الزمنية، ويعيد التحقق من صلاحيات المالك ونطاقه عند كل تشغيل.</div></>}
  </div>{formError && <div className="inline-alert error" role="alert">{formError}</div>}<div className="modal-actions"><button className="button button-light" type="button" onClick={onClose}>إلغاء</button><button className="button button-dark" type="submit" disabled={saving}>{saving ? 'جارٍ الحفظ…' : 'حفظ الإعداد'}</button></div></form></Modal>;
}

function DataTable({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) { return <section className="surface-card admin-table-card"><div className="table-topline"><div><strong>{title}</strong><span>{subtitle}</span></div><Icon name="settings" size={16} /></div>{children}</section>; }

export function OperationsPage({ access, inScopeCompanies }: OperationsPageProps) {
  const tabs = TAB_SPECS.filter((tab) => canOpenTab(access, tab));
  const [tab, setTab] = useState<OperationsTab>(tabs[0]?.id ?? 'companies');
  // Rows are kept together with the tab they belong to: a response that lands
  // after the operator switched tabs is dropped instead of being rendered by
  // another tab's renderer (the `reading 'join'` crash).
  const [dataset, setDataset] = useState<{ tab: OperationsTab; rows: unknown[] } | null>(null);
  const requestToken = useRef(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editor, setEditor] = useState<AdminRecord | null | undefined>(undefined);
  const [toast, setToast] = useState('');
  const [automationOptions, setAutomationOptions] = useState<AutomationOptions | null>(null);
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [tabSearch, setTabSearch] = useState('');
  const [workingScheduleId, setWorkingScheduleId] = useState<string | null>(null);

  const allowedTabs = useMemo(() => TAB_SPECS.filter((item) => canOpenTab(access, item)), [access]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active && !allowedTabs.some((item) => item.id === tab) && allowedTabs[0]) setTab(allowedTabs[0].id);
    });
    return () => { active = false; };
  }, [allowedTabs, tab]);

  useEffect(() => {
    let active = true;
    if (hasPermission(access, 'settings.manage')) {
      void api<unknown>('/api/staff/admin/automation-options').then((value) => { if (active) setAutomationOptions(normalizeAutomationOptions(value)); }).catch(() => { if (active) setAutomationOptions(null); });
    }
    if (hasPermission(access, 'users.view')) {
      void api<unknown>('/api/staff/users').then((value) => { if (active) setAgents(normalizeAgents(value)); }).catch(() => { if (active) setAgents([]); });
    }
    return () => { active = false; };
  }, [access]);

  const load = useCallback(async () => {
    const requested = tab;
    const current = TAB_SPECS.find((item) => item.id === requested);
    if (!current || !canOpenTab(access, current)) { setDataset(null); setLoading(false); return; }
    // Only the newest request may write state; an older tab's response arriving
    // late is ignored rather than rendered under the wrong headers.
    const token = ++requestToken.current;
    const stale = () => token !== requestToken.current;
    setLoading(true); setError('');
    if (requested === 'integrations') { setDataset({ tab: requested, rows: [] }); setLoading(false); return; }
    try {
      const payload = await fetchTabRows(requested);
      if (stale()) return;
      setDataset({ tab: requested, rows: unwrapList(payload) });
    } catch (cause) {
      if (stale()) return;
      setDataset({ tab: requested, rows: [] });
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل إعدادات مساحة العمل.');
    } finally { if (!stale()) setLoading(false); }
  }, [access, tab]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  const showSettings = tabs.length > 0;
  if (!showSettings) return <AccessDenied title="إعدادات الإدارة غير متاحة" />;

  const tabMeta = TAB_SPECS.find((item) => item.id === tab) ?? tabs[0];
  const companyName = (id: string | null | undefined) => {
    if (!id) return 'عام · جميع الشركات';
    const known = automationOptions?.companies.find((company) => company.id === id)?.name_ar
      ?? inScopeCompanies.find((company) => company.id === id)?.name_ar;
    return display(known ?? '', id.slice(0, 8));
  };
  const nameForAgent = (id: string | null | undefined) => {
    if (!id) return 'غير محدد';
    const known = automationOptions?.agents.find((agent) => agent.id === id)?.full_name
      ?? agents.find((agent) => agent.id === id)?.full_name;
    return display(known ?? '', id.slice(0, 8));
  };
  const save = async (data: Record<string, unknown>, id?: string) => {
    const endpoint = TAB_ENDPOINTS[tab];
    if (!endpoint) throw new Error('تعذر حفظ الإعداد في هذا التبويب.');
    await api(id ? `${endpoint}/${encodeURIComponent(id)}` : endpoint, { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    setEditor(undefined); setToast(tab === 'reports' ? 'تم حفظ التقرير المجدول؛ سيتحقق العامل من النطاق والصلاحيات عند التشغيل.' : 'تم حفظ الإعداد.'); await load();
  };
  const runSchedule = async (scheduleId: string) => {
    setWorkingScheduleId(scheduleId); setError('');
    try {
      await api(`/api/staff/reports/schedules/${encodeURIComponent(scheduleId)}/run`, { method: 'POST', body: jsonBody({}) });
      setToast('تمت إضافة تشغيل التقرير إلى قائمة العامل الخلفي.');
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تشغيل التقرير الآن.');
    } finally { setWorkingScheduleId(null); }
  };
  const deleteSchedule = async (scheduleId: string) => {
    if (!window.confirm('هل تريد حذف تعريف التقرير المجدول؟')) return;
    setWorkingScheduleId(scheduleId); setError('');
    try {
      await api(`/api/staff/reports/schedules/${encodeURIComponent(scheduleId)}`, { method: 'DELETE' });
      setToast('تم حذف تعريف التقرير المجدول.');
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حذف التقرير.');
    } finally { setWorkingScheduleId(null); }
  };

  const title = tabMeta?.label ?? 'الإعدادات';
  // Rows of another tab are invisible even for the single render between the tab
  // switch and the response, so no renderer can meet a foreign record shape.
  const rows = dataset && dataset.tab === tab ? dataset.rows : [];
  const pending = loading || !dataset || dataset.tab !== tab;
  const term = tabSearch.trim().toLocaleLowerCase();
  const visibleRows = term ? rows.filter((row) => safeJson(row).toLocaleLowerCase().includes(term)) : rows;
  const table = normalizeTable(tab, visibleRows);

  if (tab === 'integrations') return <div className="page-stack"><SectionTitle eyebrow="إعدادات مساحة العمل" title="العمليات والكتالوج" description="إدارة شركات التأمين، تصنيفات الطلبات، الفرق، الأتمتة، والتكاملات ضمن صلاحيتك." />
    <BrandSettings access={access} />
    {toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}
    <div className="operation-tabs" role="tablist" aria-label="إعدادات الإدارة">{tabs.map((item) => <button role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} key={item.id} type="button" onClick={() => { setTab(item.id); setTabSearch(''); }}><Icon name={item.icon} size={15} />{item.label}</button>)}</div>
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    <IntegrationsSettings companies={inScopeCompanies} />
  </div>;

  return <div className="page-stack"><SectionTitle eyebrow="إعدادات مساحة العمل" title="العمليات والكتالوج" description="إدارة شركات التأمين، تصنيفات الطلبات، الفرق، الأتمتة، والتكاملات ضمن صلاحيتك." action={tab !== 'reports' ? <button className="button button-dark" type="button" onClick={() => setEditor(null)}><Icon name="plus" size={15} /> إضافة {title}</button> : tab === 'reports' ? <button className="button button-dark" type="button" onClick={() => setEditor(null)}><Icon name="plus" size={15} /> تعريف تقرير جديد</button> : undefined} />
    <BrandSettings access={access} />
    {toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}
    <div className="operation-tabs" role="tablist" aria-label="إعدادات الإدارة">{tabs.map((item) => <button role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} key={item.id} type="button" onClick={() => { setTab(item.id); setTabSearch(''); }}><Icon name={item.icon} size={15} />{item.label}</button>)}</div>
    {tab === 'assignment' && <div className="inline-alert info"><Icon name="refresh" size={16} /> التوزيع التلقائي اختياري؛ قواعد التوزيع لا تعمل إلا عند تفعيل العامل الخلفي. سيعيد العامل التحقق من عضوية الفريق ونطاق الشركة.</div>}
    {tab === 'reports' && <div className="inline-alert info"><Icon name="calendar" size={16} /> التقارير تعمل عبر العامل الخلفي والمزوّد المُعدّ. يُعاد احتساب نافذة البيانات والتحقق من الصلاحيات والنطاق عند كل تشغيل.</div>}
    <div className="operation-tools surface-card"><div className="queue-search-wrap"><Icon name="search" size={16} /><input aria-label={`البحث في ${title}`} value={tabSearch} onChange={(event) => setTabSearch(event.target.value)} placeholder={`ابحث في ${title}…`} /></div><button className="button button-light" type="button" onClick={() => { void load(); }} disabled={loading}><Icon name="refresh" size={15} /> تحديث</button></div>
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    {table.skipped > 0 && <div className="inline-alert warning" role="status"><Icon name="shield" size={16} /> تم إخفاء {formatNumber(table.skipped)} سجل ببيانات غير مكتملة (معرّف مفقود) من العرض.</div>}
    {/* Last-resort net: normalization above means a row can never throw, and if
        a future field ever does, one table degrades instead of the whole page. */}
    <ErrorBoundary key={tab} fallback={<div className="inline-alert error" role="alert"><Icon name="shield" size={16} /> تعذر عرض الجدول بسبب بيانات غير متوقعة.<button type="button" onClick={() => window.location.reload()}>إعادة تحميل الصفحة</button></div>}>
      <DataTable title={`${title} · ${formatNumber(table.shown)}`} subtitle={tab === 'reports' ? 'التعريفات المحفوظة لحسابك' : 'الإعدادات التي يسمح بها نطاق الإدارة'}><div className="table-wrap"><table className="data-table operations-table"><thead>{tab === 'companies' ? <tr><th>الشركة</th><th>الأنواع</th><th>جهات التواصل</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'categories' ? <tr><th>التصنيف</th><th>شركة التأمين</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'sla' ? <tr><th>النطاق</th><th>الاستجابة</th><th>الحل</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'templates' ? <tr><th>عنوان الرد</th><th>الشركة</th><th>اللغات</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'teams' ? <tr><th>الفريق</th><th>القائد</th><th>الأعضاء</th><th>إجراء</th></tr> : tab === 'assignment' ? <tr><th>قاعدة المطابقة</th><th>المستهدف</th><th>الاستراتيجية</th><th>الحالة</th><th>إجراء</th></tr> : <tr><th>اسم التقرير</th><th>التكرار</th><th>المستلمون</th><th>الحالة</th><th>إجراء</th></tr>}</thead><tbody>
        {pending && <tr><td colSpan={5}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل الإعدادات…</div></td></tr>}
        {!pending && table.companies.map((row) => <tr key={row.id}><td><div className="operation-primary-cell"><span className="company-mini-mark">{initialLetter(row.name_ar)}</span><span><strong>{display(row.name_ar)}</strong><small dir="ltr">{display(row.slug)} · {display(row.name_en)}</small></span></div></td><td>{display(row.supported_types.map(typeLabel).join('، '))}</td><td dir="ltr">{display(row.contact_emails.join(', '))}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'مفعّلة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
        {!pending && table.categories.map((row) => <tr key={row.id}><td><strong>{display(row.name_ar)}</strong><small className="cell-secondary" dir="ltr">{display(row.name_en)}</small></td><td>{companyName(row.company_id)}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
        {!pending && table.sla.map((row) => <tr key={row.id}><td><strong>{companyName(row.company_id)}</strong><small className="cell-secondary">{typeLabel(row.insurance_type)} · {row.priority ?? 'كل الأولويات'}</small></td><td>{minutes(row.response_minutes)}</td><td>{minutes(row.resolution_minutes)}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشطة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
        {!pending && table.templates.map((row) => <tr key={row.id}><td><strong>{display(row.title)}</strong><small className="cell-secondary">{preview(row.body_ar)}</small></td><td>{companyName(row.company_id)}</td><td><span className="type-chip">AR</span> <span className="type-chip">EN</span></td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
        {!pending && table.teams.map((row) => <tr key={row.id}><td><strong>{display(row.name)}</strong></td><td>{nameForAgent(row.lead_staff_id)}</td><td><span className="count-pill">{formatNumber(row.member_ids.length)} أعضاء</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تحرير الفريق</button></td></tr>)}
        {!pending && table.assignment.map((row) => <tr key={row.id}><td><strong>{companyName(row.company_id)}</strong><small className="cell-secondary">{typeLabel(row.insurance_type)} · {row.priority ?? 'كل الأولويات'}</small></td><td>{row.target_team_id ? display(automationOptions?.teams.find((team) => team.id === row.target_team_id)?.name, 'فريق') : nameForAgent(row.target_agent_id)}</td><td>{row.strategy === 'round_robin' ? 'بالتناوب' : row.strategy === 'by_company' ? 'حسب الشركة' : 'الأقل حملاً'}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'مفعّلة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
        {!pending && table.reports.map((row) => <tr key={row.id}><td><strong>{display(row.name)}</strong><small className="cell-secondary">{display(row.time_zone)} · {row.filters.window_days ?? 7} يوم</small></td><td dir="ltr">{display(row.cron_expression)}</td><td dir="ltr">{display(row.recipient_emails.join(', '))}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span><small className="cell-secondary">{row.last_run_at ? `آخر تشغيل: ${formatDateTime(row.last_run_at)}` : row.last_state ?? 'لم يعمل بعد'}</small></td><td><div className="row-action-group"><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تحرير</button><button className="button button-light button-small" type="button" disabled={!row.is_active || workingScheduleId === row.id} onClick={() => { void runSchedule(row.id); }}>{workingScheduleId === row.id ? 'جارٍ…' : 'تشغيل الآن'}</button><button className="button button-quiet button-small" type="button" disabled={workingScheduleId === row.id} onClick={() => { void deleteSchedule(row.id); }}>حذف</button></div></td></tr>)}
        {!pending && table.shown === 0 && <tr><td colSpan={5}><div className="empty-table"><span className="empty-icon"><Icon name={tabMeta?.icon ?? 'settings'} size={20} /></span><strong>لا توجد إعدادات بعد</strong><span>أضف أول إعداد لهذه المساحة.</span></div></td></tr>}
      </tbody></table></div></DataTable>
    </ErrorBoundary>
    {editor !== undefined && <OperationEditor tab={tab} record={editor ?? undefined} options={automationOptions} inScopeCompanies={inScopeCompanies} agents={agents} onClose={() => setEditor(undefined)} onSaved={save} />}
  </div>;
}
