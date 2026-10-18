import { useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import type { StaffDepartment } from '../../types';
import { formatNumber } from '../../utils/date';

interface Props {
  departments: StaffDepartment[];
  canManage: boolean;
  loading?: boolean;
  onRefresh: () => Promise<unknown> | void;
}

/** Departments are organizational labels, never access-control teams. */
export function DepartmentsPanel({ departments, canManage, loading = false, onRefresh }: Props) {
  const [editing, setEditing] = useState<StaffDepartment | null | undefined>(undefined);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [isActive, setIsActive] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const openEditor = (department: StaffDepartment | null) => {
    setEditing(department);
    setName(department?.name ?? '');
    setDescription(department?.description ?? '');
    setIsActive(department?.is_active !== false);
    setError('');
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (name.trim().length < 2) return;
    setSaving(true);
    setError('');
    try {
      const path = editing ? `/api/staff/departments/${encodeURIComponent(editing.id)}` : '/api/staff/departments';
      await api(path, {
        method: editing ? 'PUT' : 'POST',
        body: jsonBody({ name: name.trim(), description: description.trim(), is_active: isActive }),
      });
      setEditing(undefined);
      setNotice(editing ? 'تم تحديث القسم.' : 'تم إنشاء القسم. يمكنك الآن ربط الموظفين به.');
      await onRefresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر حفظ القسم.');
    } finally { setSaving(false); }
  };

  const activeCount = departments.filter((department) => department.is_active !== false).length;
  const employeeCount = departments.reduce((total, department) => total + (department.employee_count ?? 0), 0);

  return <section className="surface-card department-panel" aria-labelledby="departments-heading">
    <div className="department-panel-heading">
      <div className="department-heading-title"><span className="department-heading-icon"><Icon name="building" size={22} /></span><div>
        <span className="eyebrow">هيكل الفريق</span><h2 id="departments-heading">الأقسام</h2>
        <p>نظّم أعضاء الفريق بحسب القسم دون تغيير أدوارهم أو صلاحياتهم.</p>
      </div></div>
      {canManage && <button className="button button-dark" type="button" onClick={() => openEditor(null)}><Icon name="plus" size={16} /> قسم جديد</button>}
    </div>
    {notice && <p className="inline-alert success" role="status">{notice}</p>}
    {error && editing === undefined && <p className="inline-alert error" role="alert">{error}</p>}
    <div className="department-summary" aria-label="ملخص الأقسام">
      <span><strong>{formatNumber(departments.length)}</strong> إجمالي الأقسام</span>
      <span><strong>{formatNumber(activeCount)}</strong> قسم مفعّل</span>
      <span><strong>{formatNumber(employeeCount)}</strong> موظف مرتبط</span>
    </div>
    {loading && departments.length === 0 ? <p className="department-empty">جارٍ تحميل الأقسام…</p> : departments.length === 0 ? <p className="department-empty">لا توجد أقسام بعد.</p> :
      <div className="department-list">{departments.map((department) => <article className="department-card" key={department.id}>
        <div className="department-card-top"><span className="department-card-icon"><Icon name="building" size={18} /></span><span className={`department-state${department.is_active === false ? ' is-inactive' : ''}`}><i />{department.is_active === false ? 'غير مفعّل' : 'مفعّل'}</span></div>
        <h3>{department.name}</h3><p>{department.description || 'لا يوجد وصف لهذا القسم.'}</p>
        <div className="department-card-footer"><span><Icon name="users" size={14} /> {formatNumber(department.employee_count ?? 0)} موظف</span>{canManage && <button type="button" className="department-edit" onClick={() => openEditor(department)} aria-label={`تعديل قسم ${department.name}`}>تعديل القسم</button>}</div>
      </article>)}</div>}
    {editing !== undefined && <Modal title={editing ? 'تعديل القسم' : 'إنشاء قسم جديد'} eyebrow="إدارة أقسام الموظفين" onClose={() => setEditing(undefined)} className="department-editor-dialog">
      <p className="modal-intro">القسم للتنظيم فقط. حالة الإيقاف تمنع الدعوات والتعيينات الجديدة، ولا تحذف الموظفين المرتبطين به.</p>
      <form onSubmit={(event) => { void save(event); }}>
        <label className="field-label">اسم القسم<input autoFocus required minLength={2} maxLength={120} value={name} onChange={(event) => setName(event.target.value)} placeholder="مثال: خدمة العملاء" /></label>
        <label className="field-label">الوصف (اختياري)<textarea rows={3} maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="نبذة مختصرة عن مسؤوليات القسم" /></label>
        <label className="department-active-control"><input type="checkbox" checked={isActive} onChange={(event) => setIsActive(event.target.checked)} /><span><strong>قسم مفعّل</strong><small>متاح للموظفين الجدد ونقل الموظفين إليه</small></span></label>
        {error && <p className="inline-alert error" role="alert">{error}</p>}
        <div className="modal-actions"><button type="button" className="button button-light" onClick={() => setEditing(undefined)}>إلغاء</button><button type="submit" className="button button-dark" disabled={saving || name.trim().length < 2}>{saving ? 'جارٍ الحفظ…' : editing ? 'حفظ التعديلات' : 'إنشاء القسم'}</button></div>
      </form>
    </Modal>}
  </section>;
}
