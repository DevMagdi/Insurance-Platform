import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { api, jsonBody } from './api';
import { AccessDenied, ScopeBadge } from './components/Access';
import { DateRangePicker } from './components/DateRangePicker';
import { Icon, type IconName } from './components/Icon';
import { Modal } from './components/Modal';
import { hasPermission } from './utils/access';
import { formatDateTime, presetRange } from './utils/date';
import type { AppRoute, ComplaintFilterOptions, CompanyOption, DateRange, InsuranceType, MeResponse, NotificationItem, Permission, StaffAccess } from './types';
import './staff-console.css';

const AuthPage = lazy(async () => ({ default: (await import('./features/auth/AuthPage')).AuthPage }));
const OverviewPage = lazy(async () => ({ default: (await import('./features/dashboard/OverviewPage')).OverviewPage }));
const ComplaintQueue = lazy(async () => ({ default: (await import('./features/complaints/ComplaintQueue')).ComplaintQueue }));
const TicketWorkspace = lazy(async () => ({ default: (await import('./features/complaints/TicketWorkspace')).TicketWorkspace }));
const AuditPage = lazy(async () => ({ default: (await import('./features/admin/AuditPage')).AuditPage }));
const OperationsPage = lazy(async () => ({ default: (await import('./features/admin/OperationsPage')).OperationsPage }));
const RolesPage = lazy(async () => ({ default: (await import('./features/admin/RolesPage')).RolesPage }));
const UsersPage = lazy(async () => ({ default: (await import('./features/admin/UsersPage')).UsersPage }));

type SessionState = 'loading' | 'authenticated' | 'unauthenticated';
type Theme = 'light' | 'dark';
interface NavigationItem {
  page: AppRoute['page'];
  label: string;
  icon: IconName;
  section: 'workspace' | 'admin';
  permission?: Permission;
  extraPermission?: Permission;
}

const NAVIGATION: NavigationItem[] = [
  { page: 'overview', label: 'لوحة المؤشرات', icon: 'overview', section: 'workspace', permission: 'analytics.view', extraPermission: 'complaints.view' },
  { page: 'complaints', label: 'قائمة الشكاوى', icon: 'inbox', section: 'workspace', permission: 'complaints.view' },
  { page: 'users', label: 'المستخدمون', icon: 'users', section: 'admin', permission: 'users.view' },
  { page: 'roles', label: 'الأدوار والصلاحيات', icon: 'roles', section: 'admin', permission: 'roles.manage' },
  { page: 'operations', label: 'العمليات والكتالوج', icon: 'settings', section: 'admin' },
  { page: 'audit', label: 'سجل التدقيق', icon: 'audit', section: 'admin', permission: 'audit.view' },
];
const OPERATION_REQUIREMENTS: Array<{ permissions: Permission[]; global?: boolean }> = [
  { permissions: ['companies.manage'] }, { permissions: ['categories.manage'] },
  { permissions: ['sla.manage'] }, { permissions: ['templates.manage'] },
  { permissions: ['teams.manage', 'users.view'], global: true },
  { permissions: ['settings.manage', 'complaints.assign'], global: true },
  { permissions: ['integrations.manage'] },
  { permissions: ['reports.manage', 'analytics.view', 'analytics.export'] },
];
const NOTIFICATION_LABELS: Record<string, string> = {
  status_changed: 'تغيّرت حالة شكوى', complaint_assigned: 'أُسندت إليك شكوى', access_changed: 'تم تحديث صلاحياتك', team_membership_changed: 'تم تحديث عضويتك في فريق',
};

function routeFromPath(pathname: string): AppRoute {
  const ticket = pathname.match(/^\/staff\/complaints\/([^/]+)$/);
  if (ticket?.[1]) return { page: 'ticket', id: decodeURIComponent(ticket[1]) };
  if (pathname === '/staff/complaints') return { page: 'complaints' };
  if (pathname === '/staff/users') return { page: 'users' };
  if (pathname === '/staff/roles') return { page: 'roles' };
  if (pathname === '/staff/operations') return { page: 'operations' };
  if (pathname === '/staff/audit') return { page: 'audit' };
  return { page: 'overview' };
}
function pathForRoute(route: AppRoute): string {
  if (route.page === 'ticket') return `/staff/complaints/${encodeURIComponent(route.id)}`;
  if (route.page === 'overview') return '/staff/overview';
  if (route.page === 'complaints') return '/staff/complaints';
  return `/staff/${route.page}`;
}
function activePage(route: AppRoute): AppRoute['page'] { return route.page === 'ticket' ? 'complaints' : route.page; }
function hasGlobalAdminScope(access: StaffAccess): boolean {
  return access.all_companies && access.company_ids.length === 0
    && access.insurance_types.length === 0 && access.ownership === 'all';
}
function operationsAllowed(access: StaffAccess): boolean {
  return OPERATION_REQUIREMENTS.some((requirement) =>
    (!requirement.global || hasGlobalAdminScope(access))
    && requirement.permissions.every((permission) => hasPermission(access, permission)),
  );
}
function defaultPage(access: StaffAccess): AppRoute {
  if (hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view')) return { page: 'overview' };
  if (hasPermission(access, 'complaints.view')) return { page: 'complaints' };
  if (hasPermission(access, 'users.view')) return { page: 'users' };
  if (hasPermission(access, 'roles.manage')) return { page: 'roles' };
  if (operationsAllowed(access)) return { page: 'operations' };
  if (hasPermission(access, 'audit.view')) return { page: 'audit' };
  return { page: 'overview' };
}
function readTheme(): Theme { return window.localStorage.getItem('wasiq-theme') === 'light' ? 'light' : 'dark'; }
function initials(name: string): string { return name.split(/\s+/).map((part) => part[0] ?? '').slice(0, 2).join(''); }

function CommandPalette({ access, onNavigate, onClose }: { access: StaffAccess; onNavigate: (route: AppRoute) => void; onClose: () => void }) {
  const [search, setSearch] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const entries = NAVIGATION.filter((item) => item.page !== 'operations' || operationsAllowed(access)).filter((item) => !item.permission || (hasPermission(access, item.permission) && (!item.extraPermission || hasPermission(access, item.extraPermission))));
  const options = entries.filter((item) => item.label.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const activate = (index: number) => {
    const item = options[index];
    if (item) { onNavigate({ page: item.page } as AppRoute); onClose(); }
  };
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' && options.length) {
      event.preventDefault(); event.stopPropagation(); setActiveIndex((current) => (current + 1) % options.length);
    } else if (event.key === 'ArrowUp' && options.length) {
      event.preventDefault(); event.stopPropagation(); setActiveIndex((current) => (current - 1 + options.length) % options.length);
    } else if (event.key === 'Enter' && options.length) {
      event.preventDefault(); event.stopPropagation(); activate(activeIndex);
    }
  };
  return <Modal title="مركز الأوامر" eyebrow="تنقّل سريع" onClose={onClose}>
    <div className="command-search"><Icon name="search" size={17} /><input autoFocus value={search} onChange={(event) => { setSearch(event.target.value); setActiveIndex(0); }} onKeyDown={handleKeyDown} placeholder="ابحث عن صفحة أو إجراء…" aria-label="البحث في الأوامر" aria-controls="command-results" aria-activedescendant={options[activeIndex] ? `command-option-${options[activeIndex].page}` : undefined} /></div>
    <div className="command-results" id="command-results" role="listbox">{options.map((item, index) => <button id={`command-option-${item.page}`} role="option" aria-selected={index === activeIndex} className={`command-result${index === activeIndex ? ' active' : ''}`} type="button" key={item.page} onMouseEnter={() => setActiveIndex(index)} onClick={() => activate(index)}><span className="nav-icon"><Icon name={item.icon} size={17} /></span><strong>{item.label}</strong><Icon name="arrow" size={15} /></button>)}{options.length === 0 && <p className="empty-inline">لا توجد صفحات مطابقة لصلاحياتك.</p>}</div>
    <div className="command-shortcuts-hint"><Icon name="keyboard" size={14} /> استخدم ↑ ↓ للتنقل و Enter للاختيار · Esc للإغلاق</div>
  </Modal>;
}

function NotificationPopover({ items, loading, onRead, onClose }: { items: NotificationItem[]; loading: boolean; onRead: (item: NotificationItem) => void; onClose: () => void }) {
  return <div className="notification-popover" role="dialog" aria-label="الإشعارات"><div className="popover-heading"><div><strong>الإشعارات</strong><span>{items.filter((item) => !item.read_at).length} غير مقروءة</span></div><button className="icon-button small" type="button" aria-label="إغلاق الإشعارات" onClick={onClose}><Icon name="close" size={15} /></button></div><div className="notification-list">{loading && <div className="notification-empty">جارٍ تحميل الإشعارات…</div>}{!loading && items.length === 0 && <div className="notification-empty">لا توجد إشعارات جديدة.</div>}{items.map((item) => <button className={`notification-item${item.read_at ? '' : ' notification-unread'}`} type="button" key={item.id} onClick={() => onRead(item)}><span className="notification-dot" /><span><strong>{NOTIFICATION_LABELS[item.kind] ?? item.kind.replaceAll('_', ' ')}</strong><small>{typeof item.payload.ticket_no === 'string' ? item.payload.ticket_no : formatDateTime(item.created_at)}</small></span></button>)}</div></div>;
}

function App() {
  const [access, setAccess] = useState<StaffAccess | null>(null);
  const [sessionState, setSessionState] = useState<SessionState>('loading');
  const [sessionError, setSessionError] = useState('');
  const [route, setRoute] = useState<AppRoute>(() => routeFromPath(window.location.pathname));
  const [range, setRange] = useState<DateRange>(() => presetRange('30d'));
  const [filterOptions, setFilterOptions] = useState<ComplaintFilterOptions>({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] });
  const [companyId, setCompanyId] = useState('');
  const [insuranceType, setInsuranceType] = useState<InsuranceType | ''>('');
  const [mobileNav, setMobileNav] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [notificationsLoading, setNotificationsLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [theme, setTheme] = useState<Theme>(readTheme);

  const refreshAccess = useCallback(async () => {
    try {
      const result = await api<MeResponse>('/api/me');
      if (result.kind !== 'staff') {
        setAccess(null); setSessionState('unauthenticated'); setSessionError('هذه المساحة مخصصة لفريق الوساطة.'); return;
      }
      setAccess((current) => current && JSON.stringify(current) === JSON.stringify(result.access) ? current : result.access);
      setSessionState('authenticated'); setSessionError('');
      const current = routeFromPath(window.location.pathname);
      if (current.page === 'overview' && !(hasPermission(result.access, 'analytics.view') && hasPermission(result.access, 'complaints.view'))) {
        const fallback = defaultPage(result.access);
        window.history.replaceState({}, '', pathForRoute(fallback)); setRoute(fallback);
      }
    } catch (cause) {
      setAccess(null); setSessionState('unauthenticated'); setSessionError(cause instanceof Error ? cause.message : 'سجّل الدخول لمتابعة العمل.');
    }
  }, []);
  const navigate = useCallback((next: AppRoute) => {
    const href = pathForRoute(next);
    if (window.location.pathname !== href) window.history.pushState({}, '', href);
    setRoute(next); setMobileNav(false); setNotificationsOpen(false);
  }, []);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void refreshAccess(); });
    return () => { active = false; };
  }, [refreshAccess]);
  useEffect(() => {
    if (!access) return;
    const interval = window.setInterval(() => { void refreshAccess(); }, 30_000);
    return () => window.clearInterval(interval);
  }, [access, refreshAccess]);
  useEffect(() => {
    const expired = () => { setAccess(null); setSessionState('unauthenticated'); setSessionError('انتهت جلستك. سجّل الدخول مجدداً.'); };
    const denied = () => { void refreshAccess(); setNotice('تغيّرت صلاحيات الوصول؛ تم تحديث الواجهة.'); };
    window.addEventListener('insurance:auth-expired', expired);
    window.addEventListener('insurance:permission-denied', denied);
    return () => { window.removeEventListener('insurance:auth-expired', expired); window.removeEventListener('insurance:permission-denied', denied); };
  }, [refreshAccess]);
  useEffect(() => { window.localStorage.setItem('wasiq-theme', theme); }, [theme]);
  useEffect(() => {
    const onPopState = () => setRoute(routeFromPath(window.location.pathname));
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const key = event.key.toLocaleLowerCase();
      if ((event.metaKey || event.ctrlKey) && key === 'k') { event.preventDefault(); setPaletteOpen(true); }
      else if ((event.metaKey || event.ctrlKey) && event.shiftKey && key === 'q') { event.preventDefault(); if (access && hasPermission(access, 'complaints.view')) navigate({ page: 'complaints' }); }
      else if ((event.metaKey || event.ctrlKey) && event.shiftKey && key === 'd') { event.preventDefault(); if (access) navigate(defaultPage(access)); }
      else if (key === '?' && !(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)) setShortcutsOpen(true);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [access, navigate]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      if (access && hasPermission(access, 'complaints.view')) {
        void api<ComplaintFilterOptions>('/api/staff/complaints/filter-options')
          .then((result) => { if (active) setFilterOptions(result); })
          .catch(() => { if (active) setFilterOptions({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] }); });
      } else setFilterOptions({ companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] });
    });
    return () => { active = false; };
  }, [access]);
  useEffect(() => {
    if (!access) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setNotificationsLoading(true);
      void api<NotificationItem[]>('/api/me/notifications').then((result) => { if (active) setNotifications(result); }).catch(() => { if (active) setNotifications([]); }).finally(() => { if (active) setNotificationsLoading(false); });
    });
    return () => { active = false; };
  }, [access]);

  const onAuthenticated = useCallback(async () => { await refreshAccess(); }, [refreshAccess]);
  const onReadNotification = async (item: NotificationItem) => {
    if (!item.read_at) {
      try { await api(`/api/me/notifications/${encodeURIComponent(item.id)}/read`, { method: 'PATCH', body: jsonBody({}) }); setNotifications((current) => current.map((row) => row.id === item.id ? { ...row, read_at: new Date().toISOString() } : row)); }
      catch (cause) { setNotice(cause instanceof Error ? cause.message : 'تعذر تحديث الإشعار.'); }
    }
    if (typeof item.payload.complaint_id === 'string') navigate({ page: 'ticket', id: item.payload.complaint_id });
  };
  const logout = async () => {
    try { await api('/api/auth/logout', { method: 'POST', body: jsonBody({}) }); }
    catch { /* Local state is cleared even when the server session is already expired. */ }
    setAccess(null); setSessionState('unauthenticated'); setRoute({ page: 'overview' }); setNotifications([]);
  };

  const companyNames = useMemo(() => new Map(filterOptions.companies.map((company) => [company.id, company.name_ar || company.name_en])), [filterOptions.companies]);
  const visibleNavigation = useMemo(() => !access ? [] : NAVIGATION.filter((item) => {
    if (item.page === 'operations') return operationsAllowed(access);
    return Boolean(item.permission && hasPermission(access, item.permission) && (!item.extraPermission || hasPermission(access, item.extraPermission)));
  }), [access]);
  const unread = notifications.filter((item) => !item.read_at).length;

  if (sessionState === 'loading') return <div className={`staff-console${theme === 'light' ? ' theme-light' : ''}`}><main className="session-loading"><span className="loading-spinner" /><strong>جارٍ التحقق من جلسة العمل…</strong></main></div>;
  if (!access) return <div className={`staff-console${theme === 'light' ? ' theme-light' : ''}`}><div className="session-entry">{sessionError && <p className="session-entry-notice" role="status">{sessionError}</p>}<Suspense fallback={<main className="session-loading"><span className="loading-spinner" /><strong>جارٍ تحميل تسجيل الدخول…</strong></main>}><AuthPage onAuthenticated={onAuthenticated} /></Suspense></div></div>;

  const scopeCompanies: CompanyOption[] = filterOptions.companies;
  const effectiveCompanyId = filterOptions.companies.some((company) => company.id === companyId) ? companyId : '';
  const effectiveInsuranceType = access.insurance_types.length > 0 && insuranceType && !access.insurance_types.includes(insuranceType) ? '' : insuranceType;
  const currentNav = activePage(route);
  const showDatePicker = route.page === 'overview' || route.page === 'complaints' || route.page === 'audit';
  const pageContent = route.page === 'overview'
    ? <OverviewPage access={access} range={range} companies={filterOptions.companies} companyId={effectiveCompanyId} insuranceType={effectiveInsuranceType} onCompany={setCompanyId} onInsuranceType={setInsuranceType} onOpenComplaints={() => navigate({ page: 'complaints' })} />
    : route.page === 'complaints'
      ? <ComplaintQueue access={access} range={range} options={filterOptions} companyNames={companyNames} onOpenTicket={(id) => navigate({ page: 'ticket', id })} />
      : route.page === 'ticket'
        ? <TicketWorkspace id={route.id} access={access} options={filterOptions} onBack={() => navigate({ page: 'complaints' })} onOpenTicket={(id) => navigate({ page: 'ticket', id })} onRefreshAccess={refreshAccess} />
        : route.page === 'users'
          ? <UsersPage access={access} onAccessChanged={refreshAccess} />
          : route.page === 'roles'
            ? <RolesPage access={access} />
            : route.page === 'operations'
              ? <OperationsPage access={access} range={range} inScopeCompanies={scopeCompanies} />
              : <AuditPage access={access} range={range} />;
  const navSections: Array<{ id: 'workspace' | 'admin'; title: string }> = [{ id: 'workspace', title: 'مساحة العمل' }, { id: 'admin', title: 'الإدارة والحوكمة' }];

  return <div className={`staff-console${theme === 'light' ? ' theme-light' : ''}`}><div className="app-shell" dir="rtl">
    {mobileNav && <button className="mobile-scrim" type="button" aria-label="إغلاق القائمة" onClick={() => setMobileNav(false)} />}
    <aside className={`sidebar${mobileNav ? ' sidebar-open' : ''}`}>
      <div className="sidebar-brand"><span className="brand-mark">و</span><span><strong>وَثيق</strong><small>عمليات التأمين</small></span><span className="brand-dot" /></div>
      <div className="sidebar-user"><span className="avatar-circle avatar-active">{initials(access.full_name)}</span><span><strong>{access.full_name}</strong><small>عضو فريق</small></span><button type="button" className="icon-button small sidebar-user-menu" aria-label="تسجيل الخروج" title="تسجيل الخروج" onClick={() => { void logout(); }}><Icon name="logout" size={14} /></button></div>
      <div className="sidebar-nav-scroll">{navSections.map((section) => {
        const links = visibleNavigation.filter((item) => item.section === section.id);
        if (!links.length) return null;
        return <nav className="nav-section" aria-label={section.title} key={section.id}><p className="nav-section-title">{section.title}</p>{links.map((item) => <button type="button" className={`nav-link${currentNav === item.page ? ' active' : ''}`} key={item.page} aria-current={currentNav === item.page ? 'page' : undefined} onClick={() => navigate({ page: item.page } as AppRoute)}><span className="nav-icon"><Icon name={item.icon} size={17} /></span><span>{item.label}</span>{item.page === 'complaints' && <small className="nav-shortcut">Q</small>}</button>)}</nav>;
      })}</div>
      <div className="sidebar-bottom"><div className="sidebar-scope-label"><Icon name="shield" size={14} /> النطاق الفعّال</div><ScopeBadge access={access} companies={scopeCompanies} /><div className="sidebar-footer"><span><i /> النظام متصل</span><span>v7.0</span></div></div>
    </aside>

    <div className="main-shell">
      <header className="topbar"><div className="topbar-leading"><button className="icon-button mobile-menu-button" type="button" aria-label="فتح القائمة" onClick={() => setMobileNav(true)}><Icon name="menu" size={20} /></button><div className="topbar-breadcrumb"><span>وَثيق</span><Icon name="chevron" size={13} /><strong>{NAVIGATION.find((item) => item.page === currentNav)?.label ?? (route.page === 'ticket' ? 'مساحة الشكوى' : 'الإدارة')}</strong></div></div>
        <div className="topbar-actions">{showDatePicker && <DateRangePicker value={range} onChange={setRange} />}<button className="command-trigger" type="button" onClick={() => setPaletteOpen(true)} aria-label="فتح مركز الأوامر"><Icon name="search" size={16} /><span>ابحث أو انتقل إلى…</span><kbd>⌘ K</kbd></button><div className="popover-anchor"><button className={`icon-button notification-trigger${notificationsOpen ? ' is-active' : ''}`} type="button" aria-label={`الإشعارات، ${unread} غير مقروءة`} onClick={() => setNotificationsOpen((value) => !value)}><Icon name="bell" size={18} />{unread > 0 && <i className="notification-count">{unread > 9 ? '9+' : unread}</i>}</button>{notificationsOpen && <NotificationPopover items={notifications} loading={notificationsLoading} onRead={(item) => { void onReadNotification(item); }} onClose={() => setNotificationsOpen(false)} />}</div><button className="icon-button theme-toggle" type="button" aria-label={theme === 'dark' ? 'تفعيل الوضع الفاتح' : 'تفعيل الوضع الداكن'} title="تبديل المظهر" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={17} /></button><button className="topbar-avatar avatar-circle avatar-active" type="button" onClick={() => { void logout(); }} title="تسجيل الخروج" aria-label="تسجيل الخروج">{initials(access.full_name)}</button></div>
      </header>
      <main className="main-content"><div className="main-content-inner">
        {notice && <div className="inline-alert info global-notice" role="status">{notice}<button className="notice-close" type="button" aria-label="إخفاء" onClick={() => setNotice('')}><Icon name="close" size={14} /></button></div>}
        <Suspense fallback={<div className="loading-surface"><span className="loading-spinner" /> جارٍ تحميل مساحة العمل…</div>}>
          {route.page === 'overview' && !(hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view')) ? <AccessDenied title="لوحة المؤشرات غير متاحة" /> : route.page === 'complaints' || route.page === 'ticket' ? pageContent : route.page === 'users' && !hasPermission(access, 'users.view') ? <AccessDenied title="إدارة المستخدمين غير متاحة" /> : route.page === 'roles' && !hasPermission(access, 'roles.manage') ? <AccessDenied title="إدارة الأدوار غير متاحة" /> : route.page === 'operations' && !operationsAllowed(access) ? <AccessDenied title="إعدادات الإدارة غير متاحة" /> : route.page === 'audit' && !hasPermission(access, 'audit.view') ? <AccessDenied title="سجل التدقيق غير متاح" /> : pageContent}
        </Suspense>
      </div></main>
      <footer className="app-footer"><span>وَثيق · منصة تشغيل وساطة التأمين</span><span><Icon name="shield" size={13} /> كل إجراء محمي بنطاق الوصول الفعّال</span><button type="button" onClick={() => setShortcutsOpen(true)}><Icon name="keyboard" size={13} /> اختصارات لوحة المفاتيح</button></footer>
    </div>
    {paletteOpen && <CommandPalette access={access} onNavigate={navigate} onClose={() => setPaletteOpen(false)} />}
    {shortcutsOpen && <Modal title="اختصارات لوحة المفاتيح" eyebrow="تنقّل أسرع" onClose={() => setShortcutsOpen(false)}><div className="shortcut-list"><div><span>فتح مركز الأوامر</span><kbd>⌘ K</kbd></div><div><span>قائمة الشكاوى</span><kbd>⌘ ⇧ Q</kbd></div><div><span>الصفحة الرئيسية</span><kbd>⌘ ⇧ D</kbd></div><div><span>هذه القائمة</span><kbd>?</kbd></div><div><span>إغلاق النوافذ</span><kbd>Esc</kbd></div></div></Modal>}
  </div></div>;
}

export default App;
