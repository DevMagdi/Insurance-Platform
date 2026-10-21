import { useEffect, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { navigate, usePathname } from '../lib/router';
import { hasPermission, type StaffAccess } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { usePlatformBrand } from './Branding';
import { EmptyState } from './ui';
import { Icon, type IconName } from './Icon';
import '../staff-console.css';
import '../styles/staff.css';
// Loaded last: refines the console chrome on top of the sheets above.
import '../styles/console-premium.css';

type Theme = 'light' | 'dark';

/**
 * The legacy staff pages share the console chrome. They must follow the same
 * persisted theme as the newer console (`wasiq-theme`) instead of forcing dark
 * and leaving a dark page behind a light shell.
 */
function useConsoleTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(() => (window.localStorage.getItem('wasiq-theme') === 'light' ? 'light' : 'dark'));
  useEffect(() => {
    const sync = () => setTheme(window.localStorage.getItem('wasiq-theme') === 'light' ? 'light' : 'dark');
    window.addEventListener('storage', sync);
    window.addEventListener('wasiq:theme', sync);
    return () => { window.removeEventListener('storage', sync); window.removeEventListener('wasiq:theme', sync); };
  }, []);
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    // The portal pages read this key on boot; keep both surfaces in agreement.
    try { window.localStorage.setItem('insurance-portal:theme', theme); } catch { /* storage blocked */ }
  }, [theme]);
  return theme;
}

const LINKS: Array<{ path: string; label: string; icon: IconName; permission?: string }> = [
  { path: '/staff', label: 'لوحة المؤشرات', icon: 'overview' },
  { path: '/staff/complaints', label: 'قائمة الطلبات', icon: 'inbox', permission: 'complaints.view' },
  { path: '/staff/admin', label: 'لوحة الإدارة', icon: 'shield' },
  { path: '/staff/users', label: 'المستخدمون', icon: 'users', permission: 'users.view' },
  { path: '/staff/operations', label: 'العمليات', icon: 'settings' },
  { path: '/staff/audit', label: 'سجل التدقيق', icon: 'audit', permission: 'audit.view' },
  { path: '/staff/storage', label: 'التخزين', icon: 'layers', permission: 'storage.manage' },
];

export function StaffShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const path = usePathname();
  const queryClient = useQueryClient();
  const { access, isLoading } = useStaffMe();
  const brand = usePlatformBrand();
  const theme = useConsoleTheme();

  if (isLoading) {
    return (
      <div className={`staff-console theme-${theme}`}>
        <main className="session-loading"><span className="loading-spinner" /><strong>{t('common.loading')}</strong></main>
      </div>
    );
  }

  if (!access) {
    return (
      <div className={`staff-console theme-${theme}`}>
        <main className="session-loading">
          <div className="access-denied">
            <span className="auth-brand-mark"><Icon name="shield" size={20} /></span>
            <h1>{t('staff.workspace')}</h1>
            <p>{t('staff.noAccess')}</p>
            <button type="button" className="button button-gold" onClick={() => navigate('/staff/login')}>
              {t('staff.loginTitle')}
            </button>
          </div>
        </main>
      </div>
    );
  }

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Clear local state even if the server session already expired.
    }
    queryClient.clear();
    navigate('/staff/login');
    window.location.reload();
  };

  return (
    <div className={`staff-console theme-${theme} staff-standalone`} dir="rtl">
      <div className="app-shell">
        <div className="main-shell" style={{ marginInlineStart: 0 }}>
          <header className="topbar">
            <div className="topbar-leading">
              <img className="brand-mark" src={brand.logo_url} alt="" />
              <div className="topbar-breadcrumb">
                <span>{brand.platform_name}</span>
                <Icon name="chevron" size={13} />
                <strong>مساحة الطلب</strong>
              </div>
            </div>
            <nav className="topbar-links" aria-label="staff">
              {LINKS.filter((link) => !link.permission || hasPermission(access as StaffAccess, link.permission as never)).slice(0, 5).map((link) => (
                <button key={link.path} type="button" onClick={() => navigate(link.path)} className={path === link.path ? 'is-active' : undefined}>
                  <Icon name={link.icon} size={15} />
                  {link.label}
                </button>
              ))}
            </nav>
            <div className="topbar-actions">
              <button type="button" className="button button-quiet" onClick={() => void logout()}>
                <Icon name="logout" size={14} /> {t('nav.logout')}
              </button>
            </div>
          </header>
          <main className="main-content">
            <div className="main-content-inner page-stack">{children ?? <EmptyState message={t('staff.noAccess')} />}</div>
          </main>
        </div>
      </div>
    </div>
  );
}
