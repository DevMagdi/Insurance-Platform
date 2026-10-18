import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { AccessDenied } from '../../components/Access';
import { hasPermission } from '../../utils/access';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { formatDateTime, formatNumber } from '../../utils/date';
import type { StaffAccess, StaffDepartment, StaffDirectoryEntry } from '../../types';
import { AccessEditor } from './AccessEditor';
import { StaffAvatar } from '../../components/StaffAvatar';
import { StaffProfileModal } from './StaffProfileModal';
import { DepartmentsPanel } from './DepartmentsPanel';

interface UsersPageProps {
  access: StaffAccess;
  onAccessChanged: () => Promise<void> | void;
}

export function UsersPage({ access, onAccessChanged }: UsersPageProps) {
  const [users, setUsers] = useState<StaffDirectoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [departmentFilter, setDepartmentFilter] = useState('');
  const [showInvite, setShowInvite] = useState(false);
  const [inviteName, setInviteName] = useState('');
  const [inviteEmail, setInviteEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [departments, setDepartments] = useState<StaffDepartment[]>([]);
  const [inviteDepartment, setInviteDepartment] = useState('');
  const [profileId, setProfileId] = useState<string | null>(null);
  const [selectedUser, setSelectedUser] = useState<StaffDirectoryEntry | null>(null);
  const [toast, setToast] = useState('');

  const load = useCallback(async () => {
    if (!hasPermission(access, 'users.view')) { setLoading(false); return; }
    setLoading(true);
    try {
      const [result, options] = await Promise.all([api<StaffDirectoryEntry[]>('/api/staff/users'), api<StaffDepartment[]>('/api/staff/departments')]);
      setUsers(result);
      setDepartments(options);
      setError('');
    } catch (cause) {
      setUsers([]);
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل المستخدمين.');
    } finally { setLoading(false); }
  }, [access]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  const filteredUsers = useMemo(() => users.filter((user) => {
    const matchesSearch = `${user.full_name} ${user.email} ${user.department_name ?? ''}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
    return matchesSearch && (!statusFilter || user.status === statusFilter)
      && (!departmentFilter || user.department_id === departmentFilter);
  }), [users, search, statusFilter, departmentFilter]);

  if (!hasPermission(access, 'users.view')) return <AccessDenied title="إدارة المستخدمين غير متاحة" />;

  const runAction = async (user: StaffDirectoryEntry, action: 'deactivate' | 'reactivate' | 'force-logout' | 'password-reset' | 'resend-invitation' | '2fa') => {
    const labels = {
      deactivate: 'تعطيل المستخدم', reactivate: 'تفعيل المستخدم', 'force-logout': 'إنهاء جميع الجلسات', 'password-reset': 'إرسال رابط إعادة تعيين كلمة المرور', '2fa': 'تغيير سياسة المصادقة الثنائية', 'resend-invitation': 'إعادة إرسال الدعوة',
    };
    if (action !== '2fa' && !window.confirm(`هل تريد ${labels[action]} لـ ${user.full_name}؟`)) return;
    setBusy(true);
    try {
      if (action === '2fa') {
        await api(`/api/staff/users/${encodeURIComponent(user.id)}/2fa-policy`, { method: 'PATCH', body: jsonBody({ required: !user.two_factor_required }) });
      } else {
        await api(`/api/staff/users/${encodeURIComponent(user.id)}/${action}`, { method: 'POST' });
      }
      setToast(action === '2fa' ? 'تم تحديث سياسة المصادقة الثنائية.' : `${labels[action]} — تم.`);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر تنفيذ الإجراء.');
    } finally { setBusy(false); }
  };

  const invite = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    try {
      await api('/api/staff/users/invitations', { method: 'POST', body: jsonBody({ email: inviteEmail.trim().toLowerCase(), full_name: inviteName.trim(), department_id: inviteDepartment }) });
      setShowInvite(false);
      setInviteEmail('');
      setInviteName('');
      setInviteDepartment('');
      setToast('قَبِل مزوّد البريد إرسال الدعوة إلى البريد المحدد. إذا لم تصل، راجع الرسائل غير المرغوبة وحالة المزوّد.');
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر إرسال الدعوة.');
    } finally { setBusy(false); }
  };

  const canInvite = hasPermission(access, 'users.create');
  const activeDepartments = departments.filter((department) => department.is_active !== false);
  const canManageAccess = hasPermission(access, 'users.manage_access');

  return (
    <div className="page-stack">
      <SectionTitle
        eyebrow="إدارة الهوية والوصول"
        title="المستخدمون"
        description="إدارة حسابات فريق الوساطة، الجلسات، وسياسات الأمان."
        action={canInvite ? <button className="button button-dark" type="button" onClick={() => setShowInvite(true)}><Icon name="users-plus" size={16} /> دعوة مستخدم</button> : undefined}
      />
      {toast && <div className="inline-alert success" role="status">{toast}<button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button></div>}
      {error && <div className="inline-alert error" role="alert">{error}<button type="button" onClick={() => { void load(); }}>إعادة المحاولة</button></div>}

      <section className="surface-card user-summary-strip">
        <div className="summary-stat"><span className="summary-stat-icon"><Icon name="users" size={17} /></span><div><strong>{formatNumber(users.length)}</strong><small>إجمالي المستخدمين</small></div></div>
        <div className="summary-stat"><span className="summary-stat-icon summary-stat-green"><Icon name="check" size={17} /></span><div><strong>{formatNumber(users.filter((user) => user.status === 'active').length)}</strong><small>حسابات مفعّلة</small></div></div>
        <div className="summary-stat"><span className="summary-stat-icon summary-stat-gold"><Icon name="lock" size={17} /></span><div><strong>{formatNumber(users.filter((user) => user.two_factor_required).length)}</strong><small>تتطلب المصادقة الثنائية</small></div></div>
        <div className="user-summary-note"><Icon name="shield" size={16} /> عرض المستخدمين متاح للمشرفين ذوي النطاق العام فقط.</div>
      </section>

      <div className="user-toolbar surface-card">
        <div className="queue-search-wrap"><Icon name="search" size={17} /><input aria-label="البحث عن مستخدم" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث بالاسم أو البريد الإلكتروني…" /></div>
        <label className="compact-field"><span>حالة الحساب</span><select aria-label="تصفية المستخدمين حسب الحالة" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">كل الحسابات</option><option value="active">مفعّل</option><option value="invited">بانتظار الدعوة</option><option value="disabled">معطّل</option></select></label>
        <label className="compact-field"><span>القسم</span><select aria-label="تصفية المستخدمين حسب القسم" value={departmentFilter} onChange={(event) => setDepartmentFilter(event.target.value)}><option value="">كل الأقسام</option>{departments.map((department) => <option key={department.id} value={department.id}>{department.name}{department.is_active === false ? ' (غير مفعّل)' : ''}</option>)}</select></label>
        <button className="button button-light" type="button" onClick={() => { void load(); }} disabled={loading}><Icon name="refresh" size={15} /> تحديث</button>
      </div>

      <section className="surface-card admin-table-card">
        <div className="table-topline"><div><strong>{formatNumber(filteredUsers.length)} مستخدم</strong><span>حسب نطاق الإدارة المصرّح</span></div><span className="last-login-note"><Icon name="clock" size={14} /> آخر تسجيل دخول</span></div>
        <div className="table-wrap"><table className="data-table admin-users-table"><thead><tr><th>المستخدم</th><th>القسم</th><th>الحالة</th><th>المصادقة الثنائية</th><th>آخر تسجيل دخول</th><th>إجراءات الإدارة</th></tr></thead><tbody>
          {loading && users.length === 0 && <tr><td colSpan={6}><div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل المستخدمين…</div></td></tr>}
          {!loading && filteredUsers.map((user) => {
            const isSelf = user.id === access.id;
            return <tr key={user.id}>
              <td><div className="user-cell"><button className="employee-avatar-button" type="button" aria-label={`عرض بروفايل ${user.full_name}`} onClick={() => setProfileId(user.id)}><StaffAvatar name={user.full_name} src={user.avatar_url} status={user.status} /></button><span><strong>{user.full_name}{isSelf && <small className="self-label">أنت</small>}</strong><small dir="ltr">{user.email}</small></span></div></td>
              <td>{user.department_name || 'غير محدد'}</td>
              <td><span className={`staff-status staff-${user.status}`}><i />{user.status === 'active' ? 'مفعّل' : user.status === 'invited' ? 'بانتظار الدعوة' : 'معطّل'}</span></td>
              <td><span className={`twofa-pill ${user.two_factor_enabled ? 'is-enabled' : 'is-disabled'}`}><Icon name={user.two_factor_enabled ? 'shield' : 'lock'} size={13} />{user.two_factor_enabled ? 'مفعّلة' : 'غير مفعّلة'}{user.two_factor_required && <em>إلزامية</em>}</span></td>
              <td className="date-cell">{formatDateTime(user.last_login_at)}</td>
              <td><div className="user-action-buttons">
                {canInvite && user.status === 'invited' && <button type="button" className="button button-light button-small" disabled={busy} onClick={() => { void runAction(user, 'resend-invitation'); }}>إعادة إرسال الدعوة</button>}
                {canManageAccess && !isSelf && <button className="icon-button small" title="تحرير الوصول" aria-label={`تحرير وصول ${user.full_name}`} onClick={() => setSelectedUser(user)}><Icon name="roles" size={15} /></button>}
                {hasPermission(access, 'users.manage_access') && !isSelf && <button className="icon-button small" title="إنهاء الجلسات" aria-label={`إنهاء جلسات ${user.full_name}`} disabled={busy} onClick={() => { void runAction(user, 'force-logout'); }}><Icon name="logout" size={15} /></button>}
                {hasPermission(access, 'users.edit') && !isSelf && <button className="icon-button small" title="إعادة تعيين كلمة المرور" aria-label={`إعادة تعيين كلمة مرور ${user.full_name}`} disabled={busy || user.status !== 'active'} onClick={() => { void runAction(user, 'password-reset'); }}><Icon name="refresh" size={15} /></button>}
                {hasPermission(access, 'users.manage_access') && !isSelf && <button className={`icon-button small ${user.two_factor_required ? 'is-selected' : ''}`} title={user.two_factor_required ? 'إلغاء إلزام المصادقة الثنائية' : 'إلزام المصادقة الثنائية'} aria-label={user.two_factor_required ? `إلغاء إلزام المصادقة لـ ${user.full_name}` : `إلزام المصادقة لـ ${user.full_name}`} disabled={busy} onClick={() => { void runAction(user, '2fa'); }}><Icon name="shield" size={15} /></button>}
                {hasPermission(access, 'users.deactivate') && !isSelf && user.status !== 'disabled' && <button className="icon-button small danger-icon" title="تعطيل المستخدم" aria-label={`تعطيل ${user.full_name}`} disabled={busy} onClick={() => { void runAction(user, 'deactivate'); }}><Icon name="close" size={15} /></button>}
                {hasPermission(access, 'users.edit') && !isSelf && user.status === 'disabled' && <button className="button button-light button-small" disabled={busy} onClick={() => { void runAction(user, 'reactivate'); }}>إعادة التفعيل</button>}
                {!canManageAccess && <span className="muted small-copy">عرض فقط</span>}
              </div></td>
            </tr>;
          })}
          {!loading && filteredUsers.length === 0 && <tr><td colSpan={6}><div className="empty-table"><span className="empty-icon"><Icon name="users" size={20} /></span><strong>لا يوجد مستخدمون مطابقون</strong><span>غيّر كلمات البحث أو مرشح الحالة.</span></div></td></tr>}
        </tbody></table></div>
      </section>

      <DepartmentsPanel departments={departments} canManage={hasPermission(access, 'users.edit')} loading={loading} onRefresh={load} />

      {showInvite && <Modal title="دعوة عضو جديد" eyebrow="إضافة إلى فريق الوساطة" onClose={() => setShowInvite(false)}><p className="modal-intro">ستُرسل دعوة آمنة بالبريد الإلكتروني. تعيين الدور ونطاق الشركات يتم بعد إنشاء الحساب.</p><form onSubmit={(event) => { void invite(event); }}><label className="field-label">الاسم الكامل<input autoFocus required minLength={2} maxLength={120} value={inviteName} onChange={(event) => setInviteName(event.target.value)} placeholder="الاسم كما سيظهر في مساحة العمل" /></label><label className="field-label">البريد الإلكتروني<input required type="email" maxLength={254} value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} placeholder="name@company.com" dir="ltr" /></label><label className="field-label">القسم<select aria-label="قسم المستخدم الجديد" required value={inviteDepartment} onChange={(event) => setInviteDepartment(event.target.value)}><option value="">اختر القسم</option>{activeDepartments.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}</select></label>{activeDepartments.length === 0 && <p className="inline-alert warning">أنشئ قسماً مفعّلاً قبل دعوة الموظفين.</p>}<div className="modal-actions"><button className="button button-light" type="button" onClick={() => setShowInvite(false)}>إلغاء</button><button className="button button-dark" type="submit" disabled={busy || inviteName.trim().length < 2 || !inviteEmail.includes('@') || !inviteDepartment}>{busy ? 'جارٍ الإرسال…' : 'إرسال الدعوة'}</button></div></form></Modal>}
      {profileId && <StaffProfileModal key={profileId} userId={profileId} canEdit={hasPermission(access, 'users.edit')} onClose={() => setProfileId(null)} onUpdated={load} />}
      {selectedUser && <AccessEditor user={selectedUser} onClose={() => setSelectedUser(null)} onSaved={async () => { setSelectedUser(null); setToast('تم تحديث وصول المستخدم وإبطال جلساته السابقة.'); await load(); await onAccessChanged(); }} />}
    </div>
  );
}
