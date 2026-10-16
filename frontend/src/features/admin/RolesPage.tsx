import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { formatNumber } from '../../utils/date';
import { PERMISSIONS, type Permission, type Role, type StaffAccess } from '../../types';

interface RolesPageProps { access: StaffAccess; }
const PERMISSION_LABELS: Record<Permission, string> = {
  'complaints.view': 'عرض الشكاوى', 'complaints.create': 'إنشاء شكوى', 'complaints.edit': 'تعديل الشكاوى', 'complaints.assign': 'إسناد الشكاوى', 'complaints.reassign': 'إعادة الإسناد', 'complaints.change_status': 'تغيير الحالة', 'complaints.reply_public': 'إرسال رد علني', 'complaints.add_internal_note': 'ملاحظة داخلية', 'complaints.merge': 'دمج الشكاوى', 'complaints.escalate': 'تحويل الشكوى', 'complaints.delete': 'حذف الشكاوى', 'complaints.export': 'تصدير الشكاوى', 'complaints.view_attachments': 'عرض المرفقات', 'complaints.view_full_national_id': 'عرض الهوية كاملة', 'analytics.view': 'عرض التحليلات', 'analytics.export': 'تصدير التحليلات', 'users.view': 'عرض المستخدمين', 'users.create': 'دعوة المستخدمين', 'users.edit': 'تعديل المستخدمين', 'users.deactivate': 'تعطيل المستخدمين', 'users.manage_access': 'إدارة الوصول', 'roles.manage': 'إدارة الأدوار', 'teams.manage': 'إدارة الفرق', 'companies.manage': 'إدارة الشركات', 'categories.manage': 'إدارة التصنيفات', 'sla.manage': 'إدارة قواعد SLA', 'templates.manage': 'إدارة الردود الجاهزة', 'integrations.manage': 'إدارة التكاملات', 'integrations.send_email': 'إرسال البريد', 'integrations.send_whatsapp': 'إرسال واتساب', 'integrations.ai_call': 'الاتصال الصوتي الذكي', 'reports.manage': 'إدارة التقارير المجدولة', 'audit.view': 'عرض سجل التدقيق', 'settings.manage': 'إعدادات النظام', 'storage.manage': 'استعراض مخزن الملفات',
};
const RESOURCE_LABELS: Record<string, string> = {
  complaints: 'الشكاوى', analytics: 'التحليلات', users: 'المستخدمون', roles: 'الأدوار', teams: 'الفرق', companies: 'الشركات', categories: 'التصنيفات', sla: 'اتفاقيات الخدمة', templates: 'الردود الجاهزة', integrations: 'التكاملات', reports: 'التقارير', audit: 'التدقيق', settings: 'الإعدادات', storage: 'التخزين',
};

function RoleEditor({ role, onClose, onSave }: { role: Role | null; onClose: () => void; onSave: (data: { name: string; description: string; permissions: Permission[] }, id?: string) => Promise<void> }) {
  const [name, setName] = useState(role?.name ?? '');
  const [description, setDescription] = useState(role?.description ?? '');
  const [permissions, setPermissions] = useState<Permission[]>(role?.permissions ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const grouped = useMemo(() => {
    const groups = new Map<string, Permission[]>();
    PERMISSIONS.forEach((permission) => {
      const resource = permission.split('.')[0] ?? 'other';
      groups.set(resource, [...(groups.get(resource) ?? []), permission]);
    });
    return [...groups.entries()];
  }, []);
  const toggle = (permission: Permission) => setPermissions((current) => current.includes(permission) ? current.filter((code) => code !== permission) : [...current, permission]);
  const setResource = (codes: Permission[], checked: boolean) => setPermissions((current) => {
    const keep = current.filter((code) => !codes.includes(code));
    return checked ? [...keep, ...codes] : keep;
  });
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try { await onSave({ name: name.trim(), description: description.trim(), permissions }, role?.id); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'تعذر حفظ الدور.'); }
    finally { setSaving(false); }
  };
  return <Modal title={role ? `تحرير دور ${role.name}` : 'إنشاء دور مخصص'} eyebrow="مصفوفة الصلاحيات" onClose={onClose} wide><form onSubmit={(event) => { void save(event); }}><div className="role-form-fields"><label className="field-label">اسم الدور<input autoFocus required minLength={2} maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="مثال: مدير متابعة المطالبات" /></label><label className="field-label">وصف موجز<textarea rows={2} maxLength={300} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="الغرض من هذا الدور ومسؤولياته" /></label></div><div className="role-matrix-grid">{grouped.map(([resource, codes]) => <section className="role-matrix-group" key={resource}><div className="role-matrix-heading"><strong>{RESOURCE_LABELS[resource] ?? resource}</strong><button type="button" onClick={() => setResource(codes, !codes.every((code) => permissions.includes(code)))}>{codes.every((code) => permissions.includes(code)) ? 'إلغاء تحديد الكل' : 'تحديد الكل'}</button></div>{codes.map((code) => <label className="role-permission-row" key={code}><input type="checkbox" checked={permissions.includes(code)} onChange={() => toggle(code)} /><span>{PERMISSION_LABELS[code]}</span><code>{code}</code></label>)}</section>)}</div>{error && <div className="inline-alert error" role="alert">{error}</div>}<div className="modal-actions"><span className="muted">{formatNumber(permissions.length)} صلاحية محددة</span><button className="button button-light" type="button" onClick={onClose}>إلغاء</button><button className="button button-dark" type="submit" disabled={saving || name.trim().length < 2}>{saving ? 'جارٍ الحفظ…' : 'حفظ الدور'}</button></div></form></Modal>;
}

export function RolesPage({ access }: RolesPageProps) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [activeRole, setActiveRole] = useState<Role | null | undefined>(undefined);
  const [cloneSource, setCloneSource] = useState<Role | null>(null);
  const [cloneName, setCloneName] = useState('');
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');

  const load = useCallback(async () => {
    if (!hasPermission(access, 'roles.manage')) { setLoading(false); return; }
    setLoading(true);
    try { setRoles(await api<Role[]>('/api/staff/roles')); setError(''); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'تعذر تحميل الأدوار.'); }
    finally { setLoading(false); }
  }, [access]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  if (!hasPermission(access, 'roles.manage')) return <AccessDenied title="إدارة الأدوار غير متاحة" />;

  const saveRole = async (data: { name: string; description: string; permissions: Permission[] }, id?: string) => {
    await api(id ? `/api/staff/roles/${encodeURIComponent(id)}` : '/api/staff/roles', { method: id ? 'PUT' : 'POST', body: jsonBody(data) });
    setActiveRole(undefined);
    setToast(id ? 'تم تحديث الدور وإبطال جلسات الأعضاء المتأثرين.' : 'تم إنشاء الدور المخصص.');
    await load();
  };
  const clone = async () => {
    if (!cloneSource || cloneName.trim().length < 2) return;
    setSaving(true);
    try {
      await api(`/api/staff/roles/${encodeURIComponent(cloneSource.id)}/clone`, { method: 'POST', body: jsonBody({ name: cloneName.trim() }) });
      setCloneSource(null); setCloneName(''); setToast('تم نسخ الدور. يمكنك الآن تعديل النسخة الجديدة.'); await load();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'تعذر نسخ الدور.'); }
    finally { setSaving(false); }
  };

  return <div className="page-stack"><SectionTitle eyebrow="التحكم بالوصول" title="الأدوار والصلاحيات" description="أنشئ أدواراً مخصصة بأقل قدر لازم من الصلاحيات. أدوار النظام محمية من التعديل." action={<button className="button button-dark" type="button" onClick={() => setActiveRole(null)}><Icon name="plus" size={16} /> دور مخصص جديد</button>} />{toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}{error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}
    <div className="roles-summary-strip surface-card"><div><span className="summary-stat-icon"><Icon name="roles" size={17} /></span><div><strong>{formatNumber(roles.length)}</strong><small>أدوار معرفة</small></div></div><div><span className="summary-stat-icon summary-stat-gold"><Icon name="lock" size={17} /></span><div><strong>{formatNumber(roles.filter((role) => role.is_system).length)}</strong><small>أدوار نظام محمية</small></div></div><div><span className="summary-stat-icon summary-stat-green"><Icon name="users" size={17} /></span><div><strong>{formatNumber(roles.reduce((total, role) => total + (role.users_count ?? 0), 0))}</strong><small>تعيينات أدوار</small></div></div><p><Icon name="shield" size={15} /> كل تغيير في دور يُسجّل ويُبطِل جلسات المستخدمين المرتبطين.</p></div>
    {loading && roles.length === 0 ? <div className="loading-surface"><span className="loading-spinner" /> جارٍ تحميل الأدوار…</div> : <div className="role-cards-grid">{roles.map((role) => <article className={`role-card surface-card${role.is_system ? ' system-role' : ''}`} key={role.id}><div className="role-card-top"><span className={`role-icon ${role.is_system ? 'role-icon-system' : ''}`}><Icon name={role.is_system ? 'shield' : 'roles'} size={19} /></span><span className={role.is_system ? 'system-lock-label' : 'custom-role-label'}>{role.is_system ? <><Icon name="lock" size={12} /> نظام محمي</> : 'دور مخصص'}</span></div><h3>{role.name}</h3><p>{role.description || 'لا يوجد وصف لهذا الدور.'}</p><div className="role-card-stats"><span><strong>{formatNumber(role.permissions.length)}</strong> صلاحية</span><span><strong>{formatNumber(role.users_count ?? 0)}</strong> مستخدم</span></div><div className="role-card-permissions">{role.permissions.slice(0, 4).map((permission) => <span key={permission}>{PERMISSION_LABELS[permission] ?? permission}</span>)}{role.permissions.length > 4 && <small>+{role.permissions.length - 4}</small>}</div><div className="role-card-actions">{!role.is_system && <button className="button button-light button-small" type="button" onClick={() => setActiveRole(role)}><Icon name="settings" size={14} /> تحرير الدور</button>}<button className="button button-quiet button-small" type="button" onClick={() => { setCloneSource(role); setCloneName(`${role.name} — نسخة`); }}><Icon name="copy" size={14} /> نسخ</button>{role.is_system && <span className="system-role-footnote"><Icon name="lock" size={12} /> غير قابل للتعديل</span>}</div></article>)}</div>}
    {activeRole !== undefined && <RoleEditor role={activeRole} onClose={() => setActiveRole(undefined)} onSave={saveRole} />}
    {cloneSource && <Modal title="نسخ الدور" eyebrow={`نسخة من ${cloneSource.name}`} onClose={() => setCloneSource(null)}><p className="modal-intro">سيتم إنشاء دور مخصص مستقل بنفس الصلاحيات. يمكنك تعديله قبل إسناده إلى المستخدمين.</p><label className="field-label">اسم النسخة<input autoFocus minLength={2} maxLength={80} value={cloneName} onChange={(event) => setCloneName(event.target.value)} /></label><div className="modal-actions"><button className="button button-light" type="button" onClick={() => setCloneSource(null)}>إلغاء</button><button className="button button-dark" type="button" disabled={saving || cloneName.trim().length < 2} onClick={() => { void clone(); }}>{saving ? 'جارٍ النسخ…' : 'إنشاء نسخة'}</button></div></Modal>}
  </div>;
}
