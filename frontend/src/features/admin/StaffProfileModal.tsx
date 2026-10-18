import { useEffect, useState } from 'react';
import { api, jsonBody } from '../../api';
import { Modal } from '../../components/Modal';
import { StaffAvatar } from '../../components/StaffAvatar';
import type { StaffDepartment, StaffEmployeeProfile } from '../../types';

interface Props {
  userId: string;
  canEdit?: boolean;
  onClose: () => void;
  onUpdated?: () => Promise<void> | void;
}

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
      canEdit ? api<StaffDepartment[]>('/api/staff/departments', { signal: controller.signal }) : Promise.resolve([]),
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
    setBusy(true);
    setSaved(false);
    try {
      await api(`/api/staff/users/${encodeURIComponent(userId)}/department`, { method: 'PATCH', body: jsonBody({ department_id: departmentId }) });
      setProfile((current) => current ? { ...current, department_id: departmentId, department_name: departments.find((department) => department.id === departmentId)?.name ?? current.department_name } : current);
      setError('');
      setSaved(true);
      await onUpdated?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حفظ القسم.');
    } finally { setBusy(false); }
  };

  const details = profile?.personal;
  return <Modal title="بروفايل الموظف" eyebrow="بيانات شخصية · وصول مُقيّد" onClose={onClose} wide>
    {error && <p role="alert" className="inline-alert error">{error}</p>}
    {!profile && !error && <p role="status">جارٍ تحميل البروفايل…</p>}
    {profile && <>
      <div className="user-cell employee-profile-heading"><StaffAvatar name={profile.full_name} src={profile.avatar_url} status={profile.status} /><div><h3>{profile.full_name}</h3><p dir="ltr">{profile.email}</p><p>{profile.department_name}</p></div></div>
      {!details && <p className="inline-alert">{profile.status === 'invited' ? 'لم يقبل الموظف الدعوة أو يكمل بياناته بعد.' : 'حساب سابق — لم يُستكمل البروفايل بعد.'}</p>}
      {details && <dl className="employee-profile-details">
        {[
          ['الاسم الكامل', profile.full_name], ['البريد الإلكتروني', profile.email],
          ['القسم', profile.department_name], ['الرقم القومي', details.national_id],
          ['تاريخ الميلاد', details.date_of_birth], ['رقم الهاتف', details.mobile],
          ['العنوان', details.address], ['المدينة / المحافظة', details.city],
          ['المسمى الوظيفي', details.job_title], ['هاتف الطوارئ', details.emergency_phone],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd dir="auto">{value || 'غير مسجل'}</dd></div>)}
      </dl>}
      {canEdit && <form onSubmit={(event) => { event.preventDefault(); void saveDepartment(); }} className="employee-department-form">
        <label className="field-label">قسم الموظف<select aria-label="قسم الموظف" required value={departmentId} onChange={(event) => { setDepartmentId(event.target.value); setSaved(false); }}>{departments.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}</select></label>
        <button className="button button-dark" disabled={busy || !departmentId} type="submit">{busy ? 'جارٍ الحفظ…' : 'حفظ القسم'}</button>
        {saved && <p role="status">تم حفظ القسم.</p>}
      </form>}
    </>}
  </Modal>;
}
