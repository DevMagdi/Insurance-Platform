import { useEffect, useMemo, useState } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { formatNumber } from '../../utils/date';
import { PERMISSIONS, type InsuranceType, type Permission, type PermissionEffect, type Role, type StaffAccess, type StaffDirectoryEntry } from '../../types';

interface AccessOptions {
  companies: Array<{ id: string; name_en: string; name_ar: string }>;
  teams: Array<{ id: string; name: string; member_ids: string[] }>;
  roles: Role[];
}
interface ExplainResponse {
  editable: boolean;
  access: StaffAccess;
  why: Record<string, { roles: string[]; override: PermissionEffect | null; effective: boolean }>;
}
interface AccessEditorProps {
  user: StaffDirectoryEntry;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}

const GROUPS: Array<{ id: string; label: string; permissions: Permission[] }> = [
  { id: 'complaints', label: 'الشكاوى ومساحة العمل', permissions: PERMISSIONS.filter((code) => code.startsWith('complaints.')) },
  { id: 'analytics', label: 'التحليلات والتقارير', permissions: PERMISSIONS.filter((code) => code.startsWith('analytics.')) },
  { id: 'users', label: 'المستخدمون والصلاحيات', permissions: PERMISSIONS.filter((code) => code.startsWith('users.')) },
  { id: 'roles', label: 'الأدوار', permissions: PERMISSIONS.filter((code) => code.startsWith('roles.')) },
  { id: 'teams', label: 'الفرق', permissions: PERMISSIONS.filter((code) => code.startsWith('teams.')) },
  { id: 'companies', label: 'الشركات والتصنيفات', permissions: PERMISSIONS.filter((code) => ['companies.', 'categories.', 'sla.', 'templates.'].some((prefix) => code.startsWith(prefix))) },
  { id: 'integrations', label: 'التكاملات والاتصال', permissions: PERMISSIONS.filter((code) => code.startsWith('integrations.')) },
  { id: 'reports', label: 'التقارير المجدولة', permissions: PERMISSIONS.filter((code) => code.startsWith('reports.')) },
  { id: 'audit-settings', label: 'التدقيق والإعدادات', permissions: PERMISSIONS.filter((code) => code.startsWith('audit.') || code.startsWith('settings.')) },
];
const PERMISSION_LABELS: Record<Permission, string> = {
  'complaints.view': 'عرض الشكاوى', 'complaints.create': 'إنشاء شكوى', 'complaints.edit': 'تعديل بيانات الشكوى',
  'complaints.assign': 'إسناد الشكاوى', 'complaints.reassign': 'إعادة الإسناد', 'complaints.change_status': 'تغيير الحالة',
  'complaints.reply_public': 'إرسال رد علني', 'complaints.add_internal_note': 'إضافة ملاحظة داخلية', 'complaints.merge': 'دمج الشكاوى',
  'complaints.escalate': 'تحويل الشكوى', 'complaints.delete': 'حذف الشكاوى', 'complaints.export': 'تصدير الشكاوى',
  'complaints.view_attachments': 'عرض المرفقات', 'complaints.view_full_national_id': 'عرض رقم الهوية كاملاً',
  'analytics.view': 'عرض التحليلات', 'analytics.export': 'تصدير التحليلات', 'users.view': 'عرض المستخدمين',
  'users.create': 'دعوة مستخدم', 'users.edit': 'تعديل بيانات المستخدم', 'users.deactivate': 'تعطيل المستخدم',
  'users.manage_access': 'إدارة الوصول والنطاقات', 'roles.manage': 'إدارة الأدوار', 'teams.manage': 'إدارة الفرق', 'companies.manage': 'إدارة شركات التأمين',
  'categories.manage': 'إدارة التصنيفات', 'sla.manage': 'إدارة قواعد SLA', 'templates.manage': 'إدارة الردود الجاهزة',
  'integrations.manage': 'إدارة التكاملات', 'integrations.send_email': 'إرسال البريد للشركات',
  'integrations.send_whatsapp': 'إرسال واتساب', 'integrations.ai_call': 'الاتصال الصوتي الذكي',
  'reports.manage': 'إدارة التقارير المجدولة', 'audit.view': 'عرض سجل التدقيق', 'settings.manage': 'إدارة إعدادات النظام',
};

export function AccessEditor({ user, onClose, onSaved }: AccessEditorProps) {
  const [access, setAccess] = useState<StaffAccess | null>(null);
  const [options, setOptions] = useState<AccessOptions | null>(null);
  const [overrides, setOverrides] = useState<Record<string, PermissionEffect | ''>>({});
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const [allCompanies, setAllCompanies] = useState(true);
  const [companyIds, setCompanyIds] = useState<string[]>([]);
  const [insuranceTypes, setInsuranceTypes] = useState<InsuranceType[]>([]);
  const [ownership, setOwnership] = useState<StaffAccess['ownership']>('all');
  const [teamIds, setTeamIds] = useState<string[]>([]);
  const [explanation, setExplanation] = useState<ExplainResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [previewOpen, setPreviewOpen] = useState(false);
  const [matrixFilter, setMatrixFilter] = useState('');

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [snapshot, accessOptions, explain] = await Promise.all([
          api<StaffAccess>(`/api/staff/users/${encodeURIComponent(user.id)}/access`),
          api<AccessOptions>('/api/staff/admin/access-options'),
          api<ExplainResponse>(`/api/staff/users/${encodeURIComponent(user.id)}/access/explain`),
        ]);
        if (!active) return;
        setAccess(snapshot);
        setOptions(accessOptions);
        setExplanation(explain);
        setRoleIds(snapshot.roles);
        setOverrides(Object.fromEntries(PERMISSIONS.map((permission) => [permission, snapshot.overrides[permission] ?? ''])));
        setAllCompanies(snapshot.all_companies);
        setCompanyIds(snapshot.company_ids);
        setInsuranceTypes(snapshot.insurance_types);
        setOwnership(snapshot.ownership);
        setTeamIds(snapshot.team_ids);
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : 'تعذر تحميل تفاصيل الوصول.');
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => { active = false; };
  }, [user.id]);

  const effectivePermissions = useMemo(() => {
    const roleGrants = new Set<Permission>();
    if (options) {
      options.roles.filter((role) => roleIds.includes(role.id)).forEach((role) => role.permissions.forEach((permission) => roleGrants.add(permission)));
    } else {
      access?.permissions.forEach((permission) => roleGrants.add(permission));
    }
    return PERMISSIONS.filter((permission) => {
      const override = overrides[permission];
      if (override === 'deny') return false;
      return override === 'allow' || roleGrants.has(permission);
    });
  }, [access, options, overrides, roleIds]);

  const companiesLabel = allCompanies ? 'جميع الشركات' : companyIds.map((id) => options?.companies.find((company) => company.id === id)?.name_ar ?? id.slice(0, 7)).join('، ') || 'لم تُحدّد شركات';
  const typesLabel = insuranceTypes.length === 0 ? 'كل أنواع التأمين' : insuranceTypes.map((type) => ({ health: 'طبي', general: 'عام', both: 'مشترك' })[type]).join('، ');
  const searchedGroups = GROUPS.map((group) => ({
    ...group,
    permissions: group.permissions.filter((permission) => `${PERMISSION_LABELS[permission]} ${permission}`.toLocaleLowerCase().includes(matrixFilter.toLocaleLowerCase())),
  })).filter((group) => group.permissions.length > 0);

  const save = async () => {
    if (!access) return;
    setSaving(true);
    setError('');
    const allow = PERMISSIONS.filter((permission) => overrides[permission] === 'allow');
    const deny = PERMISSIONS.filter((permission) => overrides[permission] === 'deny');
    try {
      await api(`/api/staff/users/${encodeURIComponent(user.id)}/access`, {
        method: 'PUT',
        body: jsonBody({
          role_ids: roleIds,
          allow,
          deny,
          all_companies: allCompanies,
          company_ids: allCompanies ? [] : companyIds,
          insurance_types: insuranceTypes,
          ownership,
          team_ids: teamIds,
        }),
      });
      await onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حفظ صلاحيات المستخدم.');
    } finally { setSaving(false); }
  };

  const toggleRole = (id: string) => setRoleIds((current) => current.includes(id) ? current.filter((roleId) => roleId !== id) : [...current, id]);
  const toggleCompany = (id: string) => setCompanyIds((current) => current.includes(id) ? current.filter((companyId) => companyId !== id) : [...current, id]);
  const toggleType = (value: InsuranceType) => setInsuranceTypes((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  const toggleTeam = (id: string) => setTeamIds((current) => current.includes(id) ? current.filter((teamId) => teamId !== id) : [...current, id]);
  const setGroupEffect = (permissions: Permission[], effect: PermissionEffect | '') => setOverrides((current) => ({ ...current, ...Object.fromEntries(permissions.map((permission) => [permission, effect])) }));

  return (
    <Modal title={`صلاحيات ${user.full_name}`} eyebrow="محرر الوصول الفعّال" onClose={onClose} wide>
      {loading ? <div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل سياسة الوصول…</div> : error && !access ? <div className="inline-alert error" role="alert">{error}</div> : access && (
        <>
          <div className="access-editor-layout">
            <div className="access-editor-main">
              <section className="editor-section">
                <div className="editor-section-heading"><div><h3>الأدوار</h3><p>الصلاحيات الفعّالة تبدأ من أدوار المستخدم.</p></div><span>{formatNumber(roleIds.length)} محدد</span></div>
                {!options?.roles.length ? <div className="inline-alert warning">تعذر تحميل قائمة الأدوار. سيتم الحفاظ على الأدوار الحالية دون تغيير.</div> : <div className="role-chip-grid">{options.roles.map((role) => <label className={`role-choice${roleIds.includes(role.id) ? ' selected' : ''}`} key={role.id}><input type="checkbox" checked={roleIds.includes(role.id)} onChange={() => toggleRole(role.id)} /><span className="role-choice-mark"><Icon name={role.is_system ? 'shield' : 'roles'} size={14} /></span><span><strong>{role.name}</strong><small>{role.is_system ? 'دور نظام محمي' : 'دور مخصص'}</small></span></label>)}</div>}
              </section>

              <section className="editor-section">
                <div className="editor-section-heading"><div><h3>نطاق الشركات والملكية</h3><p>يُطبّق النطاق على القوائم والبحث والتحليلات والتصدير.</p></div><span className="scope-preview-badge"><Icon name="shield" size={13} /> يُطبّق على الخادم</span></div>
                <label className={`scope-choice${allCompanies ? ' selected' : ''}`}><input type="radio" name="company-scope" checked={allCompanies} onChange={() => setAllCompanies(true)} /><span><strong>جميع الشركات</strong><small>كل شركات التأمين</small></span></label>
                <label className={`scope-choice${!allCompanies ? ' selected' : ''}`}><input type="radio" name="company-scope" checked={!allCompanies} onChange={() => { setAllCompanies(false); if (!companyIds.length && options?.companies[0]) setCompanyIds([options.companies[0].id]); }} /><span><strong>شركات محددة</strong><small>اختر شركة واحدة أو أكثر</small></span></label>
                {!allCompanies && <div className="scope-option-grid">{options?.companies.map((company) => <label className="check-row scope-check" key={company.id}><input type="checkbox" checked={companyIds.includes(company.id)} onChange={() => toggleCompany(company.id)} /><span>{company.name_ar || company.name_en}</span></label>)}</div>}
                <div className="scope-inline-heading"><strong>أنواع التأمين</strong><small>عدم تحديد نوع يعني كل الأنواع</small></div>
                <div className="scope-type-grid">{([{ value: 'health', label: 'طبي' }, { value: 'general', label: 'عام' }, { value: 'both', label: 'مشترك' }] as const).map((item) => <label className={`type-choice${insuranceTypes.includes(item.value) ? ' selected' : ''}`} key={item.value}><input type="checkbox" checked={insuranceTypes.includes(item.value)} onChange={() => toggleType(item.value)} /><span>{item.label}</span></label>)}</div>
                <div className="access-fields-grid"><label className="field-label">مستوى الملكية<select value={ownership} onChange={(event) => setOwnership(event.target.value as StaffAccess['ownership'])}><option value="own">شكاواي فقط</option><option value="team">شكاوى فريقي</option><option value="all">كل الشكاوى ضمن النطاق</option></select></label><div className="team-scope-field"><span>الفرق المسموح بها</span>{options?.teams.length ? <div className="team-checkbox-list">{options.teams.map((team) => <label className="check-row" key={team.id}><input type="checkbox" checked={teamIds.includes(team.id)} onChange={() => toggleTeam(team.id)} /><span>{team.name}</span></label>)}</div> : <small className="muted">لا توجد فرق متاحة.</small>}</div></div>
              </section>

              <section className="editor-section permission-editor-section">
                <div className="editor-section-heading"><div><h3>مصفوفة الصلاحيات</h3><p>تجاوزات المستخدم تتقدم على الأدوار؛ المنع يتقدم على السماح.</p></div><span>{formatNumber(effectivePermissions.length)} فعّالة</span></div>
                <div className="permission-matrix-tools"><div className="queue-search-wrap matrix-search"><Icon name="search" size={15} /><input aria-label="البحث في الصلاحيات" value={matrixFilter} onChange={(event) => setMatrixFilter(event.target.value)} placeholder="ابحث عن صلاحية…" /></div><div className="matrix-legend"><span className="legend-inherit">موروثة</span><span className="legend-allow">سماح</span><span className="legend-deny">منع</span></div></div>
                <div className="permission-groups">{searchedGroups.map((group) => (
                  <section className="permission-group" key={group.id}>
                    <div className="permission-group-title"><strong>{group.label}</strong><div><button type="button" onClick={() => setGroupEffect(group.permissions, 'allow')}>سماح للكل</button><button type="button" onClick={() => setGroupEffect(group.permissions, 'deny')}>منع الكل</button><button type="button" onClick={() => setGroupEffect(group.permissions, '')}>إعادة للوراثة</button></div></div>
                    {group.permissions.map((permission) => <div className="permission-row" key={permission}><div><strong>{PERMISSION_LABELS[permission]}</strong><code>{permission}</code></div><select aria-label={`تجاوز ${PERMISSION_LABELS[permission]}`} value={overrides[permission] ?? ''} onChange={(event) => setOverrides((current) => ({ ...current, [permission]: event.target.value as PermissionEffect | '' }))}><option value="">وراثة</option><option value="allow">سماح</option><option value="deny">منع</option></select><span className={`effective-status ${effectivePermissions.includes(permission) ? 'effective-on' : 'effective-off'}`}>{effectivePermissions.includes(permission) ? 'فعّالة' : 'موقوفة'}</span></div>)}
                  </section>
                ))}</div>
              </section>
            </div>

            <aside className="effective-preview-panel">
              <div className="preview-panel-heading"><span className="preview-shield"><Icon name="shield" size={17} /></span><div><p className="eyebrow">معاينة مباشرة</p><h3>الوصول الفعّال</h3></div></div>
              <div className="preview-user"><span className="avatar-circle">{user.full_name.split(' ').map((part) => part[0]).slice(0, 2).join('')}</span><div><strong>{user.full_name}</strong><small dir="ltr">{user.email}</small></div></div>
              <div className="preview-scope-card"><small>الشركات</small><strong>{companiesLabel}</strong><small>التأمين والملكية</small><strong>{typesLabel} · {ownership === 'own' ? 'ملكي' : ownership === 'team' ? 'الفريق' : 'كل النطاق'}</strong></div>
              <div className="preview-permission-count"><strong>{formatNumber(effectivePermissions.length)}</strong><span>صلاحية فعّالة</span></div>
              <div className="preview-permission-list">{effectivePermissions.slice(0, 8).map((permission) => <span key={permission}><Icon name="check" size={12} />{PERMISSION_LABELS[permission]}</span>)}{effectivePermissions.length > 8 && <small>+ {formatNumber(effectivePermissions.length - 8)} صلاحية أخرى</small>}{effectivePermissions.length === 0 && <small>لا توجد صلاحيات فعّالة.</small>}</div>
              {explanation && <div className="preview-current-state"><Icon name="refresh" size={13} /> المعاينة تستند إلى التعديلات الحالية، والتحقق النهائي من الخادم.</div>}
              <button className="button button-light full-width" type="button" onClick={() => setPreviewOpen(true)}><Icon name="external" size={14} /> عرض كـ {user.full_name.split(' ')[0]}</button>
              <div className="preview-warning"><Icon name="lock" size={13} /> المعاينة للقراءة فقط ولا تنتحل هوية المستخدم.</div>
            </aside>
          </div>
          {error && <div className="inline-alert error" role="alert">{error}</div>}
          <div className="modal-actions access-editor-actions"><span className="muted">سيتم إبطال الجلسات الحالية بعد حفظ التغييرات.</span><button className="button button-light" type="button" onClick={onClose}>إلغاء</button><button className="button button-dark" type="button" disabled={saving || (!allCompanies && companyIds.length === 0)} onClick={() => { void save(); }}>{saving ? 'جارٍ الحفظ…' : 'حفظ الوصول'}</button></div>
        </>
      )}
      {previewOpen && access && <Modal title={`معاينة مساحة ${user.full_name}`} eyebrow="عرض للقراءة فقط" onClose={() => setPreviewOpen(false)} wide><div className="view-as-preview"><div className="view-as-banner"><Icon name="lock" size={17} /><span>هذه معاينة تفسيرية لنطاق الوصول؛ لن يتم تبديل الجلسة أو تسجيل الدخول باسم المستخدم.</span></div><div className="view-as-grid"><div><small>نطاق الشركات</small><strong>{companiesLabel}</strong></div><div><small>أنواع التأمين</small><strong>{typesLabel}</strong></div><div><small>مستوى الملكية</small><strong>{ownership}</strong></div><div><small>الحالة</small><strong>{access.status === 'active' ? 'مفعّل' : access.status}</strong></div></div><h3>الصلاحيات الفعّالة في التعديلات الحالية</h3><div className="view-as-permissions">{effectivePermissions.map((permission) => <span key={permission}><Icon name="check" size={13} />{PERMISSION_LABELS[permission]}</span>)}</div></div></Modal>}
    </Modal>
  );
}
