import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { IntegrationsSettings } from './IntegrationsSettings';
import { BrandSettings } from './BrandSettings';
import { formatDateTime, formatNumber } from '../../utils/date';
import { duplicateEmailList, invalidEmailList, parseEmailList } from '../../lib/validation';
import type { CompanyOption, DateRange, InsuranceType, StaffAccess, StaffDirectoryEntry } from '../../types';

interface CompanyRecord { id: string; slug: string; name_en: string; name_ar: string; contact_emails: string[]; supported_types: InsuranceType[]; is_active: boolean; }
interface CategoryRecord { id: string; company_id: string | null; parent_id: string | null; name_en: string; name_ar: string; is_active: boolean; }
interface SlaRecord { id: string; company_id: string | null; insurance_type: InsuranceType | null; priority: string | null; response_minutes: number; resolution_minutes: number; is_active: boolean; }
interface TemplateRecord { id: string; company_id: string | null; title: string; body_en: string; body_ar: string; is_active: boolean; }
interface TeamRecord { id: string; name: string; lead_staff_id: string | null; member_ids: string[]; }
interface AssignmentRecord { id: string; company_id: string | null; insurance_type: InsuranceType | null; category_id: string | null; priority: string | null; target_team_id: string | null; target_agent_id: string | null; strategy: 'round_robin' | 'least_workload' | 'by_company'; sort_order: number; is_active: boolean; }
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

function OperationEditor({
  tab, record, options, inScopeCompanies, users, onClose, onSaved,
}: {
  tab: OperationsTab;
  record?: AdminRecord;
  options: AutomationOptions | null;
  inScopeCompanies: CompanyOption[];
  users: StaffDirectoryEntry[];
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
  const availableAgents = options?.agents ?? users.map((user) => ({ id: user.id, full_name: user.full_name }));
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
      <label className="field-label">المعرّف المختصر<input autoFocus required pattern="[a-z][a-z0-9-]{1,59}" value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="nile-assurance" dir="ltr" /></label>
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
    {tab === 'assignment' && <>{commonCompanySelect}<label className="field-label">نوع التأمين<select value={insuranceType} onChange={(event) => setInsuranceType(event.target.value)}><option value="">كل الأنواع</option>{INSURANCE_TYPES.map((type) => <option key={type.id} value={type.id}>{type.label}</option>)}</select></label><label className="field-label">التصنيف<select value={parentId} onChange={(event) => setParentId(event.target.value)}><option value="">كل التصنيفات</option>{availableCategories.map((category) => <option key={category.id} value={category.id}>{category.name_en}</option>)}</select></label><label className="field-label">الأولوية<select value={priority} onChange={(event) => setPriority(event.target.value)}><option value="">كل الأولويات</option>{PRIORITIES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label className="field-label">الفريق المستهدف<select value={targetTeamId} onChange={(event) => setTargetTeamId(event.target.value)}><option value="">بدون فريق</option>{availableTeams.map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}</select></label><label className="field-label">موظف محدد (اختياري)<select value={targetAgentId} onChange={(event) => setTargetAgentId(event.target.value)}><option value="">اختيار من الفريق أو حسب الحمل</option>{availableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.full_name}</option>)}</select></label><div className="inline-alert warning full-form-row">القاعدة تحتاج هدفاً: اختر فريقاً أو موظفاً محدداً، وإلا لن يتم توزيع أي شكوى.</div><label className="field-label">استراتيجية التوزيع<select value={strategy} onChange={(event) => setStrategy(event.target.value)}><option value="least_workload">الأقل في حجم العمل</option><option value="round_robin">بالتناوب</option><option value="by_company">حسب الشركة</option></select></label><label className="field-label">ترتيب القاعدة<input type="number" min={0} max={100000} value={sortOrder} onChange={(event) => setSortOrder(event.target.value)} /></label>{activeToggle}</>}

    {tab === 'reports' && <><label className="field-label">اسم التقرير<input autoFocus required value={name} onChange={(event) => setName(event.target.value)} /></label><label className="field-label">المستلمون<input required value={recipients} onChange={(event) => setRecipients(event.target.value)} placeholder="team@example.com" dir="ltr" /><small>عناوين مفصولة بفاصلة، وكل عنوان صالح وفريد.</small></label><label className="field-label">جدول Cron من خمسة حقول<input required value={cron} onChange={(event) => setCron(event.target.value)} placeholder="0 8 * * 1" dir="ltr" /></label><label className="field-label">المنطقة الزمنية<input required value={timeZone} onChange={(event) => setTimeZone(event.target.value)} dir="ltr" /></label><label className="field-label">نافذة البيانات<select value={windowDays} onChange={(event) => setWindowDays(event.target.value)}>{[7, 14, 30, 90, 180, 366].map((days) => <option key={days} value={days}>آخر {days} يومًا</option>)}</select></label><label className="field-label">صيغة التقرير<select value={reportFormat} onChange={(event) => setReportFormat(event.target.value as 'csv' | 'xlsx')}><option value="csv">CSV</option><option value="xlsx">Excel (XLSX)</option></select></label>{commonCompanySelect}<label className="field-label">نوع التأمين<select value={insuranceType} onChange={(event) => setInsuranceType(event.target.value)}><option value="">كل الأنواع</option>{INSURANCE_TYPES.map((type) => <option key={type.id} value={type.id}>{type.label}</option>)}</select></label>{activeToggle}<div className="inline-alert info full-form-row">يحسب الخادم موعد التشغيل من تعبير Cron والمنطقة الزمنية، ويعيد التحقق من صلاحيات المالك ونطاقه عند كل تشغيل.</div></>}
  </div>{formError && <div className="inline-alert error" role="alert">{formError}</div>}<div className="modal-actions"><button className="button button-light" type="button" onClick={onClose}>إلغاء</button><button className="button button-dark" type="submit" disabled={saving}>{saving ? 'جارٍ الحفظ…' : 'حفظ الإعداد'}</button></div></form></Modal>;
}

function DataTable({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) { return <section className="surface-card admin-table-card"><div className="table-topline"><div><strong>{title}</strong><span>{subtitle}</span></div><Icon name="settings" size={16} /></div>{children}</section>; }

export function OperationsPage({ access, inScopeCompanies }: OperationsPageProps) {
  const tabs = TAB_SPECS.filter((tab) => canOpenTab(access, tab));
  const [tab, setTab] = useState<OperationsTab>(tabs[0]?.id ?? 'companies');
  const [rows, setRows] = useState<AdminRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editor, setEditor] = useState<AdminRecord | null | undefined>(undefined);
  const [toast, setToast] = useState('');
  const [automationOptions, setAutomationOptions] = useState<AutomationOptions | null>(null);
  const [users, setUsers] = useState<StaffDirectoryEntry[]>([]);
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
      void api<AutomationOptions>('/api/staff/admin/automation-options').then((value) => { if (active) setAutomationOptions(value); }).catch(() => { if (active) setAutomationOptions(null); });
    }
    if (hasPermission(access, 'users.view')) {
      void api<StaffDirectoryEntry[]>('/api/staff/users').then((value) => { if (active) setUsers(value); }).catch(() => { if (active) setUsers([]); });
    }
    return () => { active = false; };
  }, [access]);

  const load = useCallback(async () => {
    const current = TAB_SPECS.find((item) => item.id === tab);
    if (!current || !canOpenTab(access, current)) { setLoading(false); return; }
    setLoading(true); setError('');
    if (tab === 'integrations') { setRows([]); setLoading(false); return; }
    try {
      if (tab === 'companies') setRows(await api<CompanyRecord[]>('/api/staff/admin/companies'));
      else if (tab === 'categories') setRows(await api<CategoryRecord[]>('/api/staff/admin/categories'));
      else if (tab === 'sla') setRows(await api<SlaRecord[]>('/api/staff/admin/sla-rules'));
      else if (tab === 'templates') setRows(await api<TemplateRecord[]>('/api/staff/admin/templates'));
      else if (tab === 'teams') setRows(await api<TeamRecord[]>('/api/staff/admin/teams'));
      else if (tab === 'assignment') setRows(await api<AssignmentRecord[]>('/api/staff/admin/assignment-rules'));
      else setRows(await api<ReportRecord[]>('/api/staff/reports/schedules'));
    } catch (cause) {
      setRows([]);
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل إعدادات مساحة العمل.');
    } finally { setLoading(false); }
  }, [access, tab]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  const showSettings = tabs.length > 0;
  if (!showSettings) return <AccessDenied title="إعدادات الإدارة غير متاحة" />;

  const tabMeta = TAB_SPECS.find((item) => item.id === tab) ?? tabs[0];
  const companyName = (id: string | null) => id ? automationOptions?.companies.find((company) => company.id === id)?.name_ar ?? inScopeCompanies.find((company) => company.id === id)?.name_ar ?? id.slice(0, 8) : 'عام · جميع الشركات';
  const nameForAgent = (id: string | null) => id ? automationOptions?.agents.find((agent) => agent.id === id)?.full_name ?? users.find((user) => user.id === id)?.full_name ?? id.slice(0, 8) : 'غير محدد';
  const save = async (data: Record<string, unknown>, id?: string) => {
    if (tab === 'companies') await api(id ? `/api/staff/admin/companies/${encodeURIComponent(id)}` : '/api/staff/admin/companies', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else if (tab === 'categories') await api(id ? `/api/staff/admin/categories/${encodeURIComponent(id)}` : '/api/staff/admin/categories', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else if (tab === 'sla') await api(id ? `/api/staff/admin/sla-rules/${encodeURIComponent(id)}` : '/api/staff/admin/sla-rules', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else if (tab === 'templates') await api(id ? `/api/staff/admin/templates/${encodeURIComponent(id)}` : '/api/staff/admin/templates', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else if (tab === 'teams') await api(id ? `/api/staff/admin/teams/${encodeURIComponent(id)}` : '/api/staff/admin/teams', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else if (tab === 'assignment') await api(id ? `/api/staff/admin/assignment-rules/${encodeURIComponent(id)}` : '/api/staff/admin/assignment-rules', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    else await api(id ? `/api/staff/reports/schedules/${encodeURIComponent(id)}` : '/api/staff/reports/schedules', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
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
  const visibleRows = rows.filter((row) => JSON.stringify(row).toLocaleLowerCase().includes(tabSearch.toLocaleLowerCase()));

  if (tab === 'integrations') return <div className="page-stack"><SectionTitle eyebrow="إعدادات مساحة العمل" title="العمليات والكتالوج" description="إدارة شركات التأمين، تصنيفات الشكاوى، الفرق، الأتمتة، والتكاملات ضمن صلاحيتك." />
    <BrandSettings access={access} />
    {toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}
    <div className="operation-tabs" role="tablist" aria-label="إعدادات الإدارة">{tabs.map((item) => <button role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} key={item.id} type="button" onClick={() => { setTab(item.id); setTabSearch(''); }}><Icon name={item.icon} size={15} />{item.label}</button>)}</div>
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    <IntegrationsSettings companies={inScopeCompanies} />
  </div>;

  return <div className="page-stack"><SectionTitle eyebrow="إعدادات مساحة العمل" title="العمليات والكتالوج" description="إدارة شركات التأمين، تصنيفات الشكاوى، الفرق، الأتمتة، والتكاملات ضمن صلاحيتك." action={tab !== 'reports' ? <button className="button button-dark" type="button" onClick={() => setEditor(null)}><Icon name="plus" size={15} /> إضافة {title}</button> : tab === 'reports' ? <button className="button button-dark" type="button" onClick={() => setEditor(null)}><Icon name="plus" size={15} /> تعريف تقرير جديد</button> : undefined} />
    <BrandSettings access={access} />
    {toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}
    <div className="operation-tabs" role="tablist" aria-label="إعدادات الإدارة">{tabs.map((item) => <button role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} key={item.id} type="button" onClick={() => { setTab(item.id); setTabSearch(''); }}><Icon name={item.icon} size={15} />{item.label}</button>)}</div>
    {tab === 'assignment' && <div className="inline-alert info"><Icon name="refresh" size={16} /> التوزيع التلقائي اختياري؛ قواعد التوزيع لا تعمل إلا عند تفعيل العامل الخلفي. سيعيد العامل التحقق من عضوية الفريق ونطاق الشركة.</div>}
    {tab === 'reports' && <div className="inline-alert info"><Icon name="calendar" size={16} /> التقارير تعمل عبر العامل الخلفي والمزوّد المُعدّ. يُعاد احتساب نافذة البيانات والتحقق من الصلاحيات والنطاق عند كل تشغيل.</div>}
    <div className="operation-tools surface-card"><div className="queue-search-wrap"><Icon name="search" size={16} /><input aria-label={`البحث في ${title}`} value={tabSearch} onChange={(event) => setTabSearch(event.target.value)} placeholder={`ابحث في ${title}…`} /></div><button className="button button-light" type="button" onClick={() => { void load(); }} disabled={loading}><Icon name="refresh" size={15} /> تحديث</button></div>
    {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    <DataTable title={`${title} · ${formatNumber(visibleRows.length)}`} subtitle={tab === 'reports' ? 'التعريفات المحفوظة لحسابك' : 'الإعدادات التي يسمح بها نطاق الإدارة'}><div className="table-wrap"><table className="data-table operations-table"><thead>{tab === 'companies' ? <tr><th>الشركة</th><th>الأنواع</th><th>جهات التواصل</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'categories' ? <tr><th>التصنيف</th><th>شركة التأمين</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'sla' ? <tr><th>النطاق</th><th>الاستجابة</th><th>الحل</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'templates' ? <tr><th>عنوان الرد</th><th>الشركة</th><th>اللغات</th><th>الحالة</th><th>إجراء</th></tr> : tab === 'teams' ? <tr><th>الفريق</th><th>القائد</th><th>الأعضاء</th><th>إجراء</th></tr> : tab === 'assignment' ? <tr><th>قاعدة المطابقة</th><th>المستهدف</th><th>الاستراتيجية</th><th>الحالة</th><th>إجراء</th></tr> : <tr><th>اسم التقرير</th><th>التكرار</th><th>المستلمون</th><th>الحالة</th><th>إجراء</th></tr>}</thead><tbody>
      {loading && <tr><td colSpan={5}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل الإعدادات…</div></td></tr>}
      {!loading && tab === 'companies' && (visibleRows as CompanyRecord[]).map((row) => <tr key={row.id}><td><div className="operation-primary-cell"><span className="company-mini-mark">{row.name_ar.slice(0, 1)}</span><span><strong>{row.name_ar}</strong><small dir="ltr">{row.slug} · {row.name_en}</small></span></div></td><td>{row.supported_types.map(typeLabel).join('، ')}</td><td dir="ltr">{row.contact_emails.join(', ') || '—'}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'مفعّلة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
      {!loading && tab === 'categories' && (visibleRows as CategoryRecord[]).map((row) => <tr key={row.id}><td><strong>{row.name_ar}</strong><small className="cell-secondary" dir="ltr">{row.name_en}</small></td><td>{companyName(row.company_id)}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
      {!loading && tab === 'sla' && (visibleRows as SlaRecord[]).map((row) => <tr key={row.id}><td><strong>{companyName(row.company_id)}</strong><small className="cell-secondary">{typeLabel(row.insurance_type)} · {row.priority ?? 'كل الأولويات'}</small></td><td>{formatNumber(row.response_minutes)} دقيقة</td><td>{formatNumber(row.resolution_minutes)} دقيقة</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشطة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
      {!loading && tab === 'templates' && (visibleRows as TemplateRecord[]).map((row) => <tr key={row.id}><td><strong>{row.title}</strong><small className="cell-secondary">{row.body_ar.slice(0, 56)}{row.body_ar.length > 56 ? '…' : ''}</small></td><td>{companyName(row.company_id)}</td><td><span className="type-chip">AR</span> <span className="type-chip">EN</span></td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
      {!loading && tab === 'teams' && (visibleRows as TeamRecord[]).map((row) => <tr key={row.id}><td><strong>{row.name}</strong></td><td>{nameForAgent(row.lead_staff_id)}</td><td><span className="count-pill">{formatNumber(row.member_ids.length)} أعضاء</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تحرير الفريق</button></td></tr>)}
      {!loading && tab === 'assignment' && (visibleRows as AssignmentRecord[]).map((row) => <tr key={row.id}><td><strong>{companyName(row.company_id)}</strong><small className="cell-secondary">{typeLabel(row.insurance_type)} · {row.priority ?? 'كل الأولويات'}</small></td><td>{row.target_team_id ? automationOptions?.teams.find((team) => team.id === row.target_team_id)?.name ?? 'فريق' : nameForAgent(row.target_agent_id)}</td><td>{row.strategy === 'round_robin' ? 'بالتناوب' : row.strategy === 'by_company' ? 'حسب الشركة' : 'الأقل حملاً'}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'مفعّلة' : 'متوقفة'}</span></td><td><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تعديل</button></td></tr>)}
      {!loading && tab === 'reports' && (visibleRows as ReportRecord[]).map((row) => <tr key={row.id}><td><strong>{row.name}</strong><small className="cell-secondary">{row.time_zone} · {row.filters?.window_days ?? 7} يوم</small></td><td dir="ltr">{row.cron_expression}</td><td dir="ltr">{row.recipient_emails.join(', ')}</td><td><span className={`staff-status ${row.is_active ? 'staff-active' : 'staff-disabled'}`}><i />{row.is_active ? 'نشط' : 'متوقف'}</span><small className="cell-secondary">{row.last_run_at ? `آخر تشغيل: ${formatDateTime(row.last_run_at)}` : row.last_state ?? 'لم يعمل بعد'}</small></td><td><div className="row-action-group"><button className="button button-light button-small" type="button" onClick={() => setEditor(row)}>تحرير</button><button className="button button-light button-small" type="button" disabled={!row.is_active || workingScheduleId === row.id} onClick={() => { void runSchedule(row.id); }}>{workingScheduleId === row.id ? 'جارٍ…' : 'تشغيل الآن'}</button><button className="button button-quiet button-small" type="button" disabled={workingScheduleId === row.id} onClick={() => { void deleteSchedule(row.id); }}>حذف</button></div></td></tr>)}
      {!loading && visibleRows.length === 0 && <tr><td colSpan={5}><div className="empty-table"><span className="empty-icon"><Icon name={tabMeta?.icon ?? 'settings'} size={20} /></span><strong>لا توجد إعدادات بعد</strong><span>أضف أول إعداد لهذه المساحة.</span></div></td></tr>}
    </tbody></table></div></DataTable>
    {editor !== undefined && <OperationEditor tab={tab} record={editor ?? undefined} options={automationOptions} inScopeCompanies={inScopeCompanies} users={users} onClose={() => setEditor(undefined)} onSaved={save} />}
  </div>;
}
