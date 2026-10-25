import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { api, jsonBody } from './api';
import { ensureCsrfCookie } from './lib/session';
import { AccessDenied, ScopeBadge } from './components/Access';
import { DateRangePicker } from './components/DateRangePicker';
import { Icon, type IconName } from './components/Icon';
import { usePlatformBrand } from './components/Branding';
import { useStaffNotifications } from './hooks/useStaffNotifications';
import { complaintIdForNotice, noticeLabel } from './lib/notifications';
import { Modal } from './components/Modal';
import { hasPermission } from './utils/access';
import { canViewAdminDashboard } from './features/admin/adminAccess';
import { formatDateTime, presetRange } from './utils/date';
import type { AppRoute, ComplaintFilterOptions, CompanyOption, DateRange, InsuranceType, MeResponse, NotificationItem, Permission, StaffAccess } from './types';
import './staff-console.css';
import './styles/staff.css';
import './styles/admin-identity.css';
// Loaded last: refines the console chrome on top of the sheets above.
import './styles/console-premium.css';

const AuthPage = lazy(async () => ({ default: (await import('./features/auth/AuthPage')).AuthPage }));
const OverviewPage = lazy(async () => ({ default: (await import('./features/dashboard/OverviewPage')).OverviewPage }));
const ComplaintQueue = lazy(async () => ({ default: (await import('./features/complaints/ComplaintQueue')).ComplaintQueue }));
const TicketWorkspace = lazy(async () => ({ default: (await import('./features/complaints/TicketWorkspace')).TicketWorkspace }));
const AuditPage = lazy(async () => ({ default: (await import('./features/admin/AuditPage')).AuditPage }));
const OperationsPage = lazy(async () => ({ default: (await import('./features/admin/OperationsPage')).OperationsPage }));
const RolesPage = lazy(async () => ({ default: (await import('./features/admin/RolesPage')).RolesPage }));
const UsersPage = lazy(async () => ({ default: (await import('./features/admin/UsersPage')).UsersPage }));
const CustomersPage = lazy(async () => ({ default: (await import('./features/admin/CustomersPage')).CustomersPage }));
const AdminDashboard = lazy(async () => ({ default: (await import('./features/admin/AdminDashboard')).AdminDashboard }));
const StoragePage = lazy(async () => ({ default: (await import('./features/admin/StoragePage')).StoragePage }));
const CrmPage = lazy(async () => ({ default: (await import('./features/crm/CrmPage')).CrmPage }));

const EMPTY_FILTER_OPTIONS: ComplaintFilterOptions = { companies: [], agents: [], statuses: [], priorities: [], channels: [], insurance_types: [] };
/** Filter options come from the server; tolerate a partial payload instead of blanking the console. */
function normalizeFilterOptions(value: Partial<ComplaintFilterOptions> | null | undefined): ComplaintFilterOptions {
  const list = <T,>(candidate: T[] | undefined): T[] => Array.isArray(candidate) ? candidate : [];
  return {
    companies: list(value?.companies),
    agents: list(value?.agents),
    statuses: list(value?.statuses),
    priorities: list(value?.priorities),
    channels: list(value?.channels),
    insurance_types: list(value?.insurance_types),
  };
}
type SessionState = 'loading' | 'authenticated' | 'unauthenticated';
type Theme = 'light' | 'dark';
interface NavigationItem {
  page: AppRoute['page'];
  label: string;
  icon: IconName;
  section: 'workspace' | 'more';
  permission?: Permission;
  extraPermission?: Permission;
}

/**
 * Two groups, deliberately lopsided.
 *
 * An agent opens this console to work a queue, not to administer a platform.
 * `workspace` holds the three things that belong to a shift — the queue, the
 * channels requests arrive through, and the numbers — and everything that
 * governs the platform moves behind a disclosure that is shut by default.
 * Nothing was removed: every page, route and permission check below is the
 * one that was already there.
 */
const NAVIGATION: NavigationItem[] = [
  { page: 'complaints', label: 'قائمة الطلبات', icon: 'inbox', section: 'workspace', permission: 'complaints.view' },
  { page: 'channels', label: 'إعدادات القنوات', icon: 'plug', section: 'workspace', permission: 'integrations.manage' },
  { page: 'overview', label: 'لوحة المؤشرات', icon: 'chart', section: 'workspace', permission: 'analytics.view', extraPermission: 'complaints.view' },
  { page: 'crm', label: 'المبيعات والوثائق', icon: 'building', section: 'workspace', permission: 'crm.view' },
  { page: 'admin', label: 'لوحة الإدارة', icon: 'overview', section: 'more', permission: 'analytics.view', extraPermission: 'complaints.view' },
  { page: 'users', label: 'المستخدمون', icon: 'users', section: 'more', permission: 'users.view' },
  { page: 'customers', label: 'العملاء', icon: 'users', section: 'more', permission: 'users.view' },
  { page: 'roles', label: 'الأدوار والصلاحيات', icon: 'roles', section: 'more', permission: 'roles.manage' },
  { page: 'operations', label: 'العمليات والكتالوج', icon: 'settings', section: 'more' },
  { page: 'audit', label: 'سجل التدقيق', icon: 'audit', section: 'more', permission: 'audit.view' },
  { page: 'storage', label: 'مركز التخزين', icon: 'layers', section: 'more', permission: 'storage.manage' },
];
const OPERATION_REQUIREMENTS: Array<{ permissions: Permission[]; global?: boolean }> = [
  { permissions: ['companies.manage'] }, { permissions: ['categories.manage'] },
  { permissions: ['sla.manage'] }, { permissions: ['templates.manage'] },
  { permissions: ['teams.manage', 'users.view'], global: true },
  { permissions: ['settings.manage', 'complaints.assign'], global: true },
  { permissions: ['integrations.manage'] },
  { permissions: ['reports.manage', 'analytics.view', 'analytics.export'] },
];

function routeFromPath(pathname: string): AppRoute {
  // Legacy `/dashboard*` URLs are still bookmarked; map them onto the console so
  // they never fall back to the "overview" page with the wrong ticket.
  const path = pathname.replace(/^\/dashboard(?=\/|$)/, '/staff');
  const ticket = path.match(/^\/staff\/(?:complaints|tickets)\/([^/]+)$/);
  if (ticket?.[1]) return { page: 'ticket', id: decodeURIComponent(ticket[1]) };
  if (path === '/staff/complaints') return { page: 'complaints' };
  if (path === '/staff/admin') return { page: 'admin' };
  if (path === '/staff/users') return { page: 'users' };
  if (path === '/staff/customers') return { page: 'customers' };
  if (path === '/staff/roles') return { page: 'roles' };
  if (path === '/staff/channels') return { page: 'channels' };
  if (path === '/staff/operations') return { page: 'operations' };
  if (path === '/staff/audit') return { page: 'audit' };
  if (path === '/staff/storage') return { page: 'storage' };
  if (path === '/staff/crm') return { page: 'crm' };
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
function storageAllowed(access: StaffAccess): boolean {
  return hasGlobalAdminScope(access) && hasPermission(access, 'storage.manage');
}
/**
 * Where an operator lands after signing in.
 *
 * This used to open the admin dashboard for anyone who could see it, so the
 * first thing most of the team met every morning was a governance screen.
 * The queue comes first now; the admin fallbacks below are unchanged and
 * still catch accounts that cannot see complaints at all.
 */
function defaultPage(access: StaffAccess): AppRoute {
  if (hasPermission(access, 'complaints.view')) return { page: 'complaints' };
  if (canViewAdminDashboard(access)) return { page: 'admin' };
  if (hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view')) return { page: 'overview' };
  if (hasPermission(access, 'integrations.manage')) return { page: 'channels' };
  if (hasPermission(access, 'users.view')) return { page: 'users' };
  if (hasPermission(access, 'roles.manage')) return { page: 'roles' };
  if (operationsAllowed(access)) return { page: 'operations' };
  if (storageAllowed(access)) return { page: 'storage' };
  if (hasPermission(access, 'audit.view')) return { page: 'audit' };
  return { page: 'overview' };
}
function readTheme(): Theme { return window.localStorage.getItem('wasiq-theme') === 'light' ? 'light' : 'dark'; }
function initials(name: string): string { return name.split(/\s+/).map((part) => part[0] ?? '').slice(0, 2).join(''); }

function CommandPalette({ access, onNavigate, onClose }: { access: StaffAccess; onNavigate: (route: AppRoute) => void; onClose: () => void }) {
  const [search, setSearch] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const entries = NAVIGATION
    .filter((item) => item.page !== 'admin' || canViewAdminDashboard(access))
    .filter((item) => item.page !== 'operations' || operationsAllowed(access))
    .filter((item) => item.page !== 'storage' || storageAllowed(access))
    .filter((item) => !item.permission || (hasPermission(access, item.permission) && (!item.extraPermission || hasPermission(access, item.extraPermission))));
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
  return <div className="notification-popover" role="dialog" aria-label="الإشعارات"><div className="popover-heading"><div><strong>الإشعارات</strong><span>{items.filter((item) => !item.read_at).length} غير مقروءة</span></div><button className="icon-button small" type="button" aria-label="إغلاق الإشعارات" onClick={onClose}><Icon name="close" size={15} /></button></div><div className="notification-list">{loading && <div className="notification-empty">جارٍ تحميل الإشعارات…</div>}{!loading && items.length === 0 && <div className="notification-empty">لا توجد إشعارات جديدة.</div>}{items.map((item) => <button className={`notification-item${item.read_at ? '' : ' notification-unread'}`} type="button" key={item.id} onClick={() => onRead(item)}><span className="notification-dot" /><span><strong>{noticeLabel(item.kind)}</strong><small>{typeof item.payload.ticket_no === 'string' ? `${item.payload.ticket_no} · ${formatDateTime(item.created_at)}` : formatDateTime(item.created_at)}</small></span></button>)}</div></div>;
}

function App() {
  const brand = usePlatformBrand();
  const [access, setAccess] = useState<StaffAccess | null>(null);
  const [sessionState, setSessionState] = useState<SessionState>('loading');
  const [sessionError, setSessionError] = useState('');
  const [route, setRoute] = useState<AppRoute>(() => routeFromPath(window.location.pathname));
  const [range, setRange] = useState<DateRange>(() => presetRange('30d'));
  const [filterOptions, setFilterOptions] = useState<ComplaintFilterOptions>(EMPTY_FILTER_OPTIONS);
  const [companyId, setCompanyId] = useState('');
  const [insuranceType, setInsuranceType] = useState<InsuranceType | ''>('');
  const [mobileNav, setMobileNav] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const { items: notifications, unread, loading: notificationsLoading, markRead: markNotificationRead } = useStaffNotifications(access);
  const [collapsed, setCollapsed] = useState(() => window.localStorage.getItem('wasiq-sidebar') === 'collapsed');
  // Administration is shut by default. Once an operator opens it the choice
  // sticks, so the people who do live there are not re-collapsing it daily.
  const [moreOpen, setMoreOpen] = useState(() => window.localStorage.getItem('wasiq-sidebar-more') === 'open');
  const [notice, setNotice] = useState('');
  const [theme, setTheme] = useState<Theme>(readTheme);

  const refreshAccess = useCallback(async () => {
    if (!(await ensureCsrfCookie())) {
      setAccess(null);
      setSessionState('unauthenticated');
      setSessionError('سجّل الدخول لمتابعة العمل.');
      return;
    }
    try {
      const result = await api<MeResponse>('/api/me');
      if (result.kind !== 'staff') {
        setAccess(null); setSessionState('unauthenticated'); setSessionError('هذه المساحة مخصصة لفريق الوساطة.'); return;
      }
      setAccess((current) => current && JSON.stringify(current) === JSON.stringify(result.access) ? current : result.access);
      setSessionState('authenticated'); setSessionError('');
      // `/staff` is where sign-in drops you. It used to fall through to the
      // metrics page for anyone who could read analytics, so the first screen
      // of the day was a dashboard rather than the queue. Resolve the bare
      // path against `defaultPage`; an explicit `/staff/overview` still opens
      // the metrics page.
      const landing = window.location.pathname.replace(/^\/dashboard(?=\/|$)/, '/staff');
      const isBareLanding = landing === '/staff' || landing === '/staff/';
      const current = routeFromPath(window.location.pathname);
      const overviewDenied = current.page === 'overview'
        && !(hasPermission(result.access, 'analytics.view') && hasPermission(result.access, 'complaints.view'));
      if (isBareLanding || overviewDenied) {
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
    window.localStorage.setItem('wasiq-sidebar', collapsed ? 'collapsed' : 'expanded');
  }, [collapsed]);
  useEffect(() => {
    window.localStorage.setItem('wasiq-sidebar-more', moreOpen ? 'open' : 'closed');
  }, [moreOpen]);
  useEffect(() => {
    if (!userMenuOpen) return;
    const close = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest('.user-menu-anchor')) setUserMenuOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setUserMenuOpen(false); };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', escape); };
  }, [userMenuOpen]);
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
          .then((result) => { if (active) setFilterOptions(normalizeFilterOptions(result)); })
          .catch(() => { if (active) setFilterOptions(EMPTY_FILTER_OPTIONS); });
      } else setFilterOptions(EMPTY_FILTER_OPTIONS);
    });
    return () => { active = false; };
  }, [access]);
  const onAuthenticated = useCallback(async () => { await refreshAccess(); }, [refreshAccess]);
  const onReadNotification = async (item: NotificationItem) => {
    await markNotificationRead(item);
    // Opening the complaint's chat is the whole point of the notice; the
    // link rides on the row, with the payload as a fallback.
    const complaintId = complaintIdForNotice(item);
    if (complaintId) navigate({ page: 'ticket', id: complaintId });
  };
  const logout = async () => {
    try { await api('/api/auth/logout', { method: 'POST', body: jsonBody({}) }); }
    catch { /* Local state is cleared even when the server session is already expired. */ }
    setAccess(null); setSessionState('unauthenticated'); setRoute({ page: 'overview' });
  };

  const companyNames = useMemo(() => new Map(filterOptions.companies.map((company) => [company.id, company.name_ar || company.name_en])), [filterOptions.companies]);
  const visibleNavigation = useMemo(() => !access ? [] : NAVIGATION.filter((item) => {
    if (item.page === 'operations') return operationsAllowed(access);
    if (item.page === 'admin') return canViewAdminDashboard(access);
    if (item.page === 'storage') return storageAllowed(access);
    return Boolean(item.permission && hasPermission(access, item.permission) && (!item.extraPermission || hasPermission(access, item.extraPermission)));
  }), [access]);

  if (sessionState === 'loading') return <div className={`staff-console theme-${theme}`}><main className="session-loading"><span className="loading-spinner" /><strong>جارٍ التحقق من جلسة العمل…</strong></main></div>;
  if (!access) return <div className={`staff-console theme-${theme}`}><div className="session-entry">{sessionError && <p className="session-entry-notice" role="status">{sessionError}</p>}<Suspense fallback={<main className="session-loading"><span className="loading-spinner" /><strong>جارٍ تحميل تسجيل الدخول…</strong></main>}><AuthPage onAuthenticated={onAuthenticated} /></Suspense></div></div>;

  const scopeCompanies: CompanyOption[] = filterOptions.companies;
  const effectiveCompanyId = filterOptions.companies.some((company) => company.id === companyId) ? companyId : '';
  const effectiveInsuranceType = access.insurance_types.length > 0 && insuranceType && !access.insurance_types.includes(insuranceType) ? '' : insuranceType;
  const currentNav = activePage(route);
  const showDatePicker = route.page === 'admin' || route.page === 'overview' || route.page === 'complaints' || route.page === 'audit';
  const pageContent = route.page === 'admin'
    ? <AdminDashboard access={access} range={range} onNavigate={navigate} />
    : route.page === 'overview'
    ? <OverviewPage access={access} range={range} companies={filterOptions.companies} companyId={effectiveCompanyId} insuranceType={effectiveInsuranceType} onCompany={setCompanyId} onInsuranceType={setInsuranceType} onOpenComplaints={() => navigate({ page: 'complaints' })} onOpenTicket={(id) => navigate({ page: 'ticket', id })} onOpenChannels={hasPermission(access, 'integrations.manage') ? () => navigate({ page: 'channels' }) : undefined} onOpenAudit={hasPermission(access, 'audit.view') ? () => navigate({ page: 'audit' }) : undefined} />
    : route.page === 'complaints'
      ? <ComplaintQueue access={access} range={range} options={filterOptions} companyNames={companyNames} onOpenTicket={(id) => navigate({ page: 'ticket', id })} />
      : route.page === 'ticket'
        ? <TicketWorkspace id={route.id} access={access} options={filterOptions} onBack={() => navigate({ page: 'complaints' })} onOpenTicket={(id) => navigate({ page: 'ticket', id })} onRefreshAccess={refreshAccess} />
        : route.page === 'users'
          ? <UsersPage access={access} onAccessChanged={refreshAccess} />
          : route.page === 'customers'
            ? <CustomersPage access={access} />
            : route.page === 'roles'
            ? <RolesPage access={access} />
            : route.page === 'channels'
            ? <OperationsPage access={access} range={range} inScopeCompanies={scopeCompanies} focus="integrations" />
            : route.page === 'operations'
              ? <OperationsPage access={access} range={range} inScopeCompanies={scopeCompanies} />
              : route.page === 'storage'
                ? <StoragePage access={access} />
                : route.page === 'crm'
                  ? <CrmPage access={access} />
                  : <AuditPage access={access} range={range} />;
  const workspaceLinks = visibleNavigation.filter((item) => item.section === 'workspace');
  const moreLinks = visibleNavigation.filter((item) => item.section === 'more');
  // A collapsed group must never hide where you actually are.
  const currentIsAdmin = moreLinks.some((item) => item.page === currentNav);
  const moreExpanded = moreOpen || currentIsAdmin;
  const navLink = (item: NavigationItem) => (
    <button
      type="button"
      className={`nav-link${currentNav === item.page ? ' active' : ''}`}
      key={item.page}
      aria-current={currentNav === item.page ? 'page' : undefined}
      onClick={() => navigate({ page: item.page } as AppRoute)}
    >
      <span className="nav-icon"><Icon name={item.icon} size={17} /></span>
      <span>{item.label}</span>
      {item.page === 'complaints' && <small className="nav-shortcut">Q</small>}
    </button>
  );

  return <div className={`staff-console theme-${theme}`}><div className="app-shell" dir="rtl">
    {mobileNav && <button className="mobile-scrim" type="button" aria-label="إغلاق القائمة" onClick={() => setMobileNav(false)} />}
    <aside className={`sidebar${mobileNav ? ' sidebar-open' : ''}${collapsed ? ' is-collapsed' : ''}`}>
      <div className="sidebar-brand"><img className="brand-mark" src={brand.logo_url} alt="" /><span><strong>{brand.platform_name}</strong><small>عمليات التأمين</small></span><span className="brand-dot" /></div>
      <button
        className="sidebar-collapse"
        type="button"
        aria-label={collapsed ? 'توسيع الشريط الجانبي' : 'طي الشريط الجانبي'}
        aria-expanded={!collapsed}
        title={collapsed ? 'توسيع الشريط الجانبي' : 'طي الشريط الجانبي'}
        onClick={() => setCollapsed((value) => !value)}
      >
        <Icon name="chevron" size={13} />
      </button>
      <div className="sidebar-user"><span className="avatar-circle avatar-active">{initials(access.full_name)}</span><span><strong>{access.full_name}</strong><small>عضو فريق</small></span><button type="button" className="icon-button small sidebar-user-menu" aria-label="تسجيل الخروج" title="تسجيل الخروج" onClick={() => { void logout(); }}><Icon name="logout" size={14} /></button></div>
      <div className="sidebar-nav-scroll">
        {workspaceLinks.length > 0 && (
          <nav className="nav-section" aria-label="مساحة العمل">
            <p className="nav-section-title">مساحة العمل</p>
            {workspaceLinks.map(navLink)}
          </nav>
        )}
        {moreLinks.length > 0 && (
          <nav className={`nav-section nav-section-more${moreExpanded ? ' is-open' : ''}`} aria-label="المزيد">
            <button
              type="button"
              className="nav-more-toggle"
              aria-expanded={moreExpanded}
              aria-controls="nav-more-items"
              onClick={() => setMoreOpen((value) => !value)}
            >
              <span>المزيد</span>
              <Icon name="chevron" size={13} />
            </button>
            <div className="nav-more-items" id="nav-more-items" hidden={!moreExpanded}>
              {moreLinks.map(navLink)}
            </div>
          </nav>
        )}
      </div>
      <div className="sidebar-bottom"><div className="sidebar-scope-label"><Icon name="shield" size={14} /> النطاق الفعّال</div><ScopeBadge access={access} companies={scopeCompanies} /><div className="sidebar-footer"><span><i /> النظام متصل</span><span>v7.0</span></div></div>
    </aside>

    <div className="main-shell">
      <header className="topbar"><div className="topbar-leading"><button className="icon-button mobile-menu-button" type="button" aria-label="فتح القائمة" onClick={() => setMobileNav(true)}><Icon name="menu" size={20} /></button><div className="topbar-breadcrumb"><span>{brand.platform_name}</span><Icon name="chevron" size={13} /><strong>{NAVIGATION.find((item) => item.page === currentNav)?.label ?? (route.page === 'ticket' ? 'مساحة الطلب' : 'الإدارة')}</strong></div></div>
        <div className="topbar-actions">{showDatePicker && <DateRangePicker value={range} onChange={setRange} />}<button className="command-trigger" type="button" onClick={() => setPaletteOpen(true)} aria-label="فتح مركز الأوامر"><Icon name="search" size={16} /><span>ابحث أو انتقل إلى…</span><kbd>⌘ K</kbd></button><div className="popover-anchor"><button className={`icon-button notification-trigger${notificationsOpen ? ' is-active' : ''}`} type="button" aria-label={`الإشعارات، ${unread} غير مقروءة`} onClick={() => setNotificationsOpen((value) => !value)}><Icon name="bell" size={18} />{unread > 0 && <i className="notification-count">{unread > 9 ? '9+' : unread}</i>}</button>{notificationsOpen && <NotificationPopover items={notifications} loading={notificationsLoading} onRead={(item) => { void onReadNotification(item); }} onClose={() => setNotificationsOpen(false)} />}</div><button className="icon-button theme-toggle" type="button" aria-label={theme === 'dark' ? 'تفعيل الوضع الفاتح' : 'تفعيل الوضع الداكن'} title="تبديل المظهر" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={17} /></button>
          <div className="popover-anchor user-menu-anchor">
            <button className="topbar-avatar avatar-circle avatar-active" type="button" aria-haspopup="menu" aria-expanded={userMenuOpen} aria-label="قائمة الحساب" title={access.full_name} onClick={() => setUserMenuOpen((value) => !value)}>{initials(access.full_name)}</button>
            {userMenuOpen && (
              <div className="user-menu" role="menu">
                <div className="user-menu-head"><strong>{access.full_name}</strong><span>{access.email}</span></div>
                {canViewAdminDashboard(access) && <button type="button" role="menuitem" onClick={() => { setUserMenuOpen(false); navigate({ page: 'admin' }); }}><Icon name="overview" size={14} /> لوحة الإدارة</button>}
                <button type="button" role="menuitem" onClick={() => { setTheme((value) => value === 'dark' ? 'light' : 'dark'); }}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={14} /> {theme === 'dark' ? 'الوضع الفاتح' : 'الوضع الداكن'}</button>
                <button type="button" role="menuitem" onClick={() => { setUserMenuOpen(false); setShortcutsOpen(true); }}><Icon name="keyboard" size={14} /> اختصارات لوحة المفاتيح</button>
                <button type="button" role="menuitem" onClick={() => { void logout(); }}><Icon name="logout" size={14} /> تسجيل الخروج</button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="main-content"><div className="main-content-inner">
        {notice && <div className="inline-alert info global-notice" role="status">{notice}<button className="notice-close" type="button" aria-label="إخفاء" onClick={() => setNotice('')}><Icon name="close" size={14} /></button></div>}
        <Suspense fallback={<div className="loading-surface"><span className="loading-spinner" /> جارٍ تحميل مساحة العمل…</div>}>
          {route.page === 'admin' && !canViewAdminDashboard(access) ? <AccessDenied title="لوحة الإدارة غير متاحة" /> : route.page === 'overview' && !(hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view')) ? <AccessDenied title="لوحة المؤشرات غير متاحة" /> : route.page === 'complaints' || route.page === 'ticket' ? pageContent : route.page === 'users' && !hasPermission(access, 'users.view') ? <AccessDenied title="إدارة المستخدمين غير متاحة" /> : route.page === 'roles' && !hasPermission(access, 'roles.manage') ? <AccessDenied title="إدارة الأدوار غير متاحة" /> : route.page === 'channels' && !hasPermission(access, 'integrations.manage') ? <AccessDenied title="إعدادات القنوات غير متاحة" /> : route.page === 'operations' && !operationsAllowed(access) ? <AccessDenied title="إعدادات الإدارة غير متاحة" /> : route.page === 'storage' && !storageAllowed(access) ? <AccessDenied title="مركز التخزين غير متاح" /> : route.page === 'audit' && !hasPermission(access, 'audit.view') ? <AccessDenied title="سجل التدقيق غير متاح" /> : route.page === 'crm' && !hasPermission(access, 'crm.view') ? <AccessDenied title="المبيعات والوثائق غير متاحة" /> : pageContent}
        </Suspense>
      </div></main>
      <footer className="app-footer"><span>{brand.platform_name}</span><span><Icon name="shield" size={13} /> كل إجراء محمي بنطاق الوصول الفعّال</span><button type="button" onClick={() => setShortcutsOpen(true)}><Icon name="keyboard" size={13} /> اختصارات لوحة المفاتيح</button></footer>
    </div>
    {paletteOpen && <CommandPalette access={access} onNavigate={navigate} onClose={() => setPaletteOpen(false)} />}
    {shortcutsOpen && <Modal title="اختصارات لوحة المفاتيح" eyebrow="تنقّل أسرع" onClose={() => setShortcutsOpen(false)}><div className="shortcut-list"><div><span>فتح مركز الأوامر</span><kbd>⌘ K</kbd></div><div><span>قائمة الطلبات</span><kbd>⌘ ⇧ Q</kbd></div><div><span>الصفحة الرئيسية</span><kbd>⌘ ⇧ D</kbd></div><div><span>هذه القائمة</span><kbd>?</kbd></div><div><span>إغلاق النوافذ</span><kbd>Esc</kbd></div></div></Modal>}
  </div></div>;
}

export default App;
