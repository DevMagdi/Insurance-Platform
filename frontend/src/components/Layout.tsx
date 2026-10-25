import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import {
  ArrowUpRight,
  ChevronRight,
  FilePlus2,
  HelpCircle,
  LayoutGrid,
  LogOut,
  Menu,
  Moon,
  Plus,
  Search,
  ShieldCheck,
  Sun,
  UserRound,
  X,
} from 'lucide-react';
import { applyLanguage, type AppLanguage } from '../i18n';
import { api } from '../lib/api';
import { navigate, usePathname } from '../lib/router';
import { useMe } from '../hooks/useMe';
import { usePlatformBrand } from './Branding';

const PRIMARY_NAV = [
  { path: '/', key: 'nav.home', icon: LayoutGrid },
  { path: '/complaints/new', key: 'nav.new', icon: FilePlus2 },
  { path: '/tickets', key: 'nav.tickets', icon: Search },
  { path: '/track', key: 'nav.track', icon: ChevronRight },
] as const;

function BrandMark({ size = 36, logoUrl }: { size?: number; logoUrl: string }) {
  return <img src={logoUrl} width={size} height={size} className="brand-mark" alt="" aria-hidden="true" />;
}

function useClickOutside(onOutside: () => void, active: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!active) return;
    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onOutside();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOutside();
    };
    document.addEventListener('mousedown', handler);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', handler);
      document.removeEventListener('keydown', escape);
    };
  }, [active, onOutside]);
  return ref;
}

export function Layout({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const path = usePathname();
  const { me } = useMe();
  const brand = usePlatformBrand();
  const queryClient = useQueryClient();
  const language = (i18n.language === 'en' ? 'en' : 'ar') as AppLanguage;
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'));
  // Menu state remembers the route it was opened on, so any navigation closes
  // it without an effect that would cascade a second render.
  const [menuState, setMenuState] = useState({ open: false, path });
  const [mobileState, setMobileState] = useState({ open: false, path });
  const menuOpen = menuState.open && menuState.path === path;
  const mobileOpen = mobileState.open && mobileState.path === path;
  const closeMenu = () => setMenuState({ open: false, path });
  const closeMobile = () => setMobileState({ open: false, path });
  const menuRef = useClickOutside(closeMenu, menuOpen);

  useEffect(() => {
    applyLanguage(language);
  }, [language]);

  const toggleLanguage = () => {
    const next: AppLanguage = language === 'ar' ? 'en' : 'ar';
    applyLanguage(next);
    void i18n.changeLanguage(next);
  };

  const toggleTheme = () => {
    const next = !document.documentElement.classList.contains('dark');
    document.documentElement.classList.toggle('dark', next);
    try {
      localStorage.setItem('insurance-portal:theme', next ? 'dark' : 'light');
    } catch {
      // Storage may be blocked; the class still applies for this session.
    }
    setDark(next);
  };

  const go = (next: string) => (event: React.MouseEvent) => {
    event.preventDefault();
    navigate(next);
    setMobileState({ open: false, path });
    setMenuState({ open: false, path });
  };

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Clear local state even if the server session already expired.
    }
    queryClient.clear();
    navigate('/');
    window.location.reload();
  };

  // Console screens ship their own full-bleed shell (sidebar, topbar, theme).
  // Wrapping them in the customer `max-w-6xl` column squeezed the console into a
  // 1120px island with 160px gutters on a 1440px screen.
  if ((path.startsWith('/staff') || path.startsWith('/dashboard')) && path !== '/staff/login') {
    return <>{children}</>;
  }

  if (path.startsWith('/staff') || path.startsWith('/dashboard') || path === '/auth/staff' || path === '/auth') {
    return (
      <div className="portal-shell">
        <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-[radial-gradient(60%_100%_at_50%_0%,rgba(198, 167, 94,0.1),transparent)]" />
        <main className="relative mx-auto w-full max-w-6xl px-4 pb-12 pt-10">{children}</main>
      </div>
    );
  }

  const initials = (me?.full_name ?? '')
    .split(/\s+/)
    .map((part) => part[0] ?? '')
    .slice(0, 2)
    .join('');

  return (
    <div className="portal-shell">
      <a href="#main" className="skip-link">
        {language === 'ar' ? 'تخطَّ إلى المحتوى' : 'Skip to content'}
      </a>

      <header className="site-header">
        <div className="site-header-inner">
          <a href="/" className="brand" onClick={go('/')} aria-label={brand.platform_name}>
            <BrandMark logoUrl={brand.logo_url} />
            <span className="brand-copy">
              <strong>{brand.platform_name}</strong>
              <small>{t('tagline')}</small>
            </span>
          </a>

          <nav className="site-nav" aria-label="primary">
            {PRIMARY_NAV.map((item) => {
              const active = item.path === '/' ? path === '/' : path.startsWith(item.path);
              return (
                <a
                  key={item.path}
                  href={item.path}
                  onClick={go(item.path)}
                  className={active ? 'is-active' : undefined}
                  aria-current={active ? 'page' : undefined}
                >
                  {t(item.key)}
                </a>
              );
            })}
          </nav>

          <div className="site-actions">
            <button type="button" className="lang-switch" onClick={toggleLanguage} aria-label={t('common.language')}>
              {t('common.language')}
            </button>
            <button
              type="button"
              className="icon-btn"
              onClick={toggleTheme}
              aria-label={dark ? t('common.light') : t('common.dark')}
              title={dark ? t('common.light') : t('common.dark')}
            >
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button
              type="button"
              className="icon-btn mobile-menu-btn"
              onClick={() => setMobileState({ open: true, path })}
              aria-label="menu"
            >
              <Menu size={18} />
            </button>
            {me ? (
              <div className="site-account" ref={menuRef}>
                <button
                  type="button"
                  className="account-trigger"
                  onClick={() => setMenuState({ open: !menuOpen, path })}
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                >
                  <span className="avatar" aria-hidden="true">{initials || <UserRound size={12} />}</span>
                  <span>{me.full_name.split(' ')[0]}</span>
                </button>
                {menuOpen && (
                  <div className="account-menu" role="menu">
                    <div className="account-menu-head">
                      <strong>{me.full_name}</strong>
                      <span dir="ltr">{me.mobile}</span>
                    </div>
                    {[
                      { path: '/tickets', label: t('nav.tickets'), icon: LayoutGrid },
                      { path: '/profile', label: t('nav.profile'), icon: UserRound },
                      { path: '/complaints/new', label: t('nav.new'), icon: FilePlus2 },
                    ].map((item) => {
                      const Icon = item.icon;
                      return (
                        <a key={item.path} href={item.path} role="menuitem" onClick={go(item.path)}>
                          <Icon size={14} />
                          {item.label}
                        </a>
                      );
                    })}
                    <button type="button" role="menuitem" className="danger" onClick={() => void logout()}>
                      <LogOut size={14} />
                      {t('nav.logout')}
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <button type="button" className="btn btn-secondary btn-sm site-signin" onClick={() => navigate('/login')}>
                {t('nav.login')}
                <ArrowUpRight size={13} />
              </button>
            )}
          </div>
        </div>
      </header>

      <main id="main" className="portal-main">
        <div className="portal-main-inner">{children}</div>
      </main>

      <footer className="site-footer">
        <div className="container">
          <div className="footer-grid">
            <div className="footer-brand">
              <a href="/" className="brand" onClick={go('/')}>
                <BrandMark size={32} logoUrl={brand.logo_url} />
                <span className="brand-copy">
                  <strong>{brand.platform_name}</strong>
                  <small>{t('tagline')}</small>
                </span>
              </a>
              <p>{t('footer.about')}</p>
            </div>
            <div className="footer-col">
              <h4>{t('footer.platform')}</h4>
              <ul>
                <li><a href="/complaints/new" onClick={go('/complaints/new')}>{t('nav.new')}</a></li>
                <li><a href="/track" onClick={go('/track')}>{t('nav.track')}</a></li>
                <li><a href="/tickets" onClick={go('/tickets')}>{t('nav.tickets')}</a></li>
              </ul>
            </div>
            <div className="footer-col">
              <h4>{t('footer.account')}</h4>
              <ul>
                <li><a href="/login" onClick={go('/login')}>{t('nav.login')}</a></li>
                <li><a href="/profile" onClick={go('/profile')}>{t('nav.profile')}</a></li>
                <li><button type="button" onClick={() => navigate('/track')}>{t('nav.track')}</button></li>
              </ul>
            </div>
            <div className="footer-col">
              <h4>{t('footer.support')}</h4>
              <ul>
                <li><span>{t('footer.hours')}</span></li>
                <li><span dir="ltr">care@wathiq.example</span></li>
                <li><span className="footer-compliance"><ShieldCheck size={13} /> {t('footer.compliance')}</span></li>
              </ul>
            </div>
          </div>
          <div className="footer-bottom">
            <span>© {new Date().getFullYear()} {brand.platform_name} — {t('footer.rights')}</span>
            <span className="row">
              <span>{t('footer.privacy')}</span>
              <span>{t('footer.terms')}</span>
              <span className="footer-compliance"><ShieldCheck size={12} /> {t('footer.encrypted')}</span>
            </span>
          </div>
        </div>
      </footer>

      <nav className="mobile-bottom-nav" aria-label="mobile">
        <a href="/" onClick={go('/')} className={path === '/' ? 'is-active' : undefined}>
          <LayoutGrid size={18} />
          <span>{t('nav.home')}</span>
        </a>
        <a href="/tickets" onClick={go('/tickets')} className={path.startsWith('/tickets') ? 'is-active' : undefined}>
          <Search size={18} />
          <span>{t('nav.tickets')}</span>
        </a>
        <a href="/complaints/new" onClick={go('/complaints/new')} className="fab" aria-label={t('nav.new')}>
          <Plus size={22} />
        </a>
        <a href="/track" onClick={go('/track')} className={path === '/track' ? 'is-active' : undefined}>
          <ChevronRight size={18} />
          <span>{t('nav.track')}</span>
        </a>
        <a href={me ? '/profile' : '/login'} onClick={go(me ? '/profile' : '/login')} className={path === '/profile' || path === '/login' ? 'is-active' : undefined}>
          <UserRound size={18} />
          <span>{me ? t('nav.profile') : t('nav.login')}</span>
        </a>
      </nav>

      {mobileOpen && (
        <div className="mobile-sheet" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && closeMobile()}>
          <div className="mobile-sheet-panel" role="dialog" aria-modal="true" aria-label={brand.platform_name}>
            <div className="mobile-sheet-grabber" />
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
              <span className="brand">
                <BrandMark size={32} logoUrl={brand.logo_url} />
                <span className="brand-copy"><strong>{brand.platform_name}</strong><small>{t('tagline')}</small></span>
              </span>
              <button type="button" className="icon-btn" aria-label={t('common.cancel')} onClick={closeMobile}>
                <X size={18} />
              </button>
            </div>
            {PRIMARY_NAV.map((item) => {
              const Icon = item.icon;
              return (
                <a key={item.path} href={item.path} onClick={go(item.path)}>
                  <Icon size={17} />
                  {t(item.key)}
                </a>
              );
            })}
            <a href="/profile" onClick={go('/profile')}>
              <UserRound size={17} />
              {t('nav.profile')}
            </a>
            <a href="/track" onClick={go('/track')}>
              <HelpCircle size={17} />
              {t('footer.support')}
            </a>
            <div className="row" style={{ marginTop: 12, paddingTop: 14, borderTop: '1px solid var(--line)' }}>
              <button type="button" className="lang-switch" onClick={toggleLanguage}>{t('common.language')}</button>
              <button type="button" className="lang-switch" onClick={toggleTheme}>
                {dark ? <Sun size={13} /> : <Moon size={13} />}
                {dark ? t('common.light') : t('common.dark')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
