import { useEffect, useState } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { StaffAvatar } from '../../components/StaffAvatar';
import type { Permission, StaffDepartment, StaffEmployeeProfile } from '../../types';
import { formatDateTime, formatNumber } from '../../utils/date';
import { PERMISSION_LABELS } from './permissionLabels';

interface Props {
  userId: string;
  canEdit?: boolean;
  onClose: () => void;
  onUpdated?: () => Promise<void> | void;
}

const ACCOUNT_STATUS: Record<string, string> = {
  active: 'حساب مفعّل', invited: 'بانتظار الدعوة', disabled: 'حساب معطّل',
};

export function StaffProfileModal({ userId, canEdit = false, onClose, onUpdated }: Props) {
  const [profile, setProfile] = useState<StaffEmployeeProfile | null>(null);
  const [departments, setDepartments] = useState<StaffDepartment[]>([]);
  const [departmentId, setDepartmentId] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      api<StaffEmployeeProfile>(`/api/staff/users/${encodeURIComponent(userId)}/profile`, { signal: controller.signal }),
      canEdit ? api<StaffDepartment[]>('/api/staff/departments', { signal: controller.signal }).catch(() => []) : Promise.resolve([]),
    ]).then(([employee, options]) => {
      if (!controller.signal.aborted) {
        setProfile(employee);
        setDepartmentId(employee.department_id);
        setDepartments(options);
      }
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'تعذر تحميل البروفايل.');
    });
    return () => controller.abort();
  }, [userId, canEdit]);

  const saveDepartment = async () => {
    if (!profile || !departmentId || departmentId === profile.department_id) return;
    setBusy(true);
    setSaved(false);
    try {
      await api(`/api/staff/users/${encodeURIComponent(userId)}/department`, { method: 'PATCH', body: jsonBody({ department_id: departmentId }) });
      const next = departments.find((department) => department.id === departmentId);
      setProfile((current) => current ? {
        ...current, department_id: departmentId, department_name: next?.name ?? current.department_name,
        department_description: next?.description ?? '', department_is_active: next?.is_active ?? true,
      } : current);
      setError('');
      setSaved(true);
      await onUpdated?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حفظ القسم.');
    } finally { setBusy(false); }
  };

  const details = profile?.personal;
  const roles = profile?.roles ?? [];
  const permissions = profile?.permissions ?? [];
  const availableDepartments = departments.filter((department) => department.is_active !== false || department.id === profile?.department_id);

  return <Modal title="بروفايل الموظف" eyebrow="بيانات شخصية · وصول مُقيّد" onClose={onClose} wide className="staff-profile-dialog">
    {error && <p role="alert" className="inline-alert error">{error}</p>}
    {!profile && !error && <div className="loading-surface compact-loading" role="status"><span className="loading-spinner" /> جارٍ تحميل البروفايل…</div>}
    {profile && <>
      <header className="employee-profile-hero">
        <div className="employee-profile-avatar"><StaffAvatar name={profile.full_name} src={profile.avatar_url} status={profile.status} /></div>
        <div className="employee-profile-identity"><span className="eyebrow">بطاقة عضو الفريق</span><h3>{profile.full_name}</h3>
          <p>{details?.job_title ? `${details.job_title} · ` : ''}{profile.department_name}</p>
          <span className="employee-profile-email" dir="ltr">{profile.email}</span>
        </div>
        <span className={`employee-profile-status status-${profile.status}`}><i />{ACCOUNT_STATUS[profile.status] ?? profile.status}</span>
      </header>

      <div className="employee-profile-quickfacts">
        <div><span className="profile-fact-icon"><Icon name="building" size={19} /></span><span><small>القسم</small><strong>{profile.department_name}</strong></span></div>
        <div><span className="profile-fact-icon"><Icon name="roles" size={19} /></span><span><small>الأدوار</small><strong>{formatNumber(roles.length)} أدوار</strong></span></div>
        <div><span className="profile-fact-icon"><Icon name="shield" size={19} /></span><span><small>الصلاحيات الفعّالة</small><strong>{formatNumber(permissions.length)} صلاحية</strong></span></div>
      </div>

      <div className="employee-profile-layout">
        <div className="employee-profile-main">
          <section className="employee-profile-section" aria-labelledby="profile-personal-heading">
            <div className="profile-section-heading"><span className="profile-section-icon"><Icon name="users" size={18} /></span><div><h4 id="profile-personal-heading">البيانات الشخصية</h4><p>المعلومات المسجلة لعضو الفريق</p></div></div>
            {!details ? <p className="employee-profile-empty">{profile.status === 'invited' ? 'لم يقبل الموظف الدعوة أو يكمل بياناته بعد.' : 'لم يُستكمل البروفايل بعد.'}</p> :
              <dl className="employee-profile-details">
                {[
                  ['الاسم الكامل', profile.full_name, false],
                  ['الرقم القومي', details.national_id, true],
                  ['تاريخ الميلاد', details.date_of_birth, true],
                  ['رقم الموبايل', details.mobile, true],
                  ['المدينة / المحافظة', details.city, false],
                  ['العنوان', details.address, false],
                  ['المسمى الوظيفي', details.job_title, false],
                  ['هاتف الطوارئ', details.emergency_phone, true],
                ].map(([label, value, latin]) => <div key={String(label)}><dt>{label}</dt><dd dir={latin ? 'ltr' : 'auto'}>{value || 'غير مسجل'}</dd></div>)}
              </dl>}
          </section>

          <section className="employee-profile-section" aria-labelledby="profile-roles-heading">
            <div className="profile-section-heading"><span className="profile-section-icon"><Icon name="roles" size={18} /></span><div><h4 id="profile-roles-heading">الأدوار والصلاحيات</h4><p>الوصول الفعّال للحساب، وليس عضوية القسم</p></div></div>
            {roles.length ? <div className="profile-role-list">{roles.map((role) => <div className="profile-role" key={role.id}><Icon name={role.is_system ? 'shield' : 'roles'} size={17} /><span><strong>{role.name}</strong><small>{role.description || (role.is_system ? 'دور نظام' : 'دور مخصص')}</small></span></div>)}</div> : <p className="employee-profile-empty">لم يتم تعيين أي دور بعد.</p>}
            <div className="profile-permissions"><strong>{formatNumber(permissions.length)} صلاحية فعّالة</strong><div>{permissions.slice(0, 8).map((code) => <span key={code}>{PERMISSION_LABELS[code as Permission] ?? code}</span>)}{permissions.length > 8 && <small>+ {formatNumber(permissions.length - 8)} أخرى</small>}</div></div>
          </section>
        </div>

        <aside className="employee-profile-aside">
          <section className="employee-profile-section" aria-labelledby="profile-account-heading">
            <div className="profile-section-heading"><span className="profile-section-icon"><Icon name="lock" size={18} /></span><div><h4 id="profile-account-heading">معلومات الحساب</h4><p>الحالة وسياسات الأمان</p></div></div>
            <dl className="profile-account-list">
              <div><dt>حالة الحساب</dt><dd>{ACCOUNT_STATUS[profile.status] ?? profile.status}</dd></div>
              <div><dt>المصادقة الثنائية</dt><dd>{profile.two_factor_enabled ? 'مفعّلة' : 'غير مفعّلة'}{profile.two_factor_required ? ' · إلزامية' : ''}</dd></div>
              <div><dt>تاريخ إنشاء الحساب</dt><dd>{formatDateTime(profile.created_at)}</dd></div>
              <div><dt>آخر تسجيل دخول</dt><dd>{formatDateTime(profile.last_login_at)}</dd></div>
              <div><dt>اكتمال البروفايل</dt><dd>{formatDateTime(profile.profile_completed_at)}</dd></div>
            </dl>
          </section>
          <section className="employee-profile-section" aria-labelledby="profile-department-heading">
            <div className="profile-section-heading"><span className="profile-section-icon"><Icon name="building" size={18} /></span><div><h4 id="profile-department-heading">القسم الوظيفي</h4><p>بيانات تنظيمية، لا تمنح صلاحيات</p></div></div>
            <div className="employee-department-current"><strong>{profile.department_name}</strong><small>{profile.department_description || 'لا يوجد وصف لهذا القسم.'}</small>{profile.department_is_active === false && <em>القسم غير مفعّل</em>}</div>
            {canEdit && <form onSubmit={(event) => { event.preventDefault(); void saveDepartment(); }} className="employee-department-form">
              <label className="field-label">قسم الموظف<select aria-label="قسم الموظف" required value={departmentId} onChange={(event) => { setDepartmentId(event.target.value); setSaved(false); }}>{availableDepartments.map((department) => <option key={department.id} value={department.id}>{department.name}{department.is_active === false ? ' (غير مفعّل)' : ''}</option>)}{!availableDepartments.some((department) => department.id === profile.department_id) && <option value={profile.department_id}>{profile.department_name}</option>}</select></label>
              <button className="button button-dark" disabled={busy || !departmentId || departmentId === profile.department_id} type="submit">{busy ? 'جارٍ الحفظ…' : 'حفظ القسم'}</button>
              {saved && <p role="status" className="profile-saved"><Icon name="check" size={14} /> تم حفظ القسم.</p>}
            </form>}
          </section>
        </aside>
      </div>
    </>}
  </Modal>;
}
