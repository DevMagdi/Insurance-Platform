import { useEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { applyLanguage, type AppLanguage } from '../i18n';
import { api } from '../lib/api';
import { navigate, usePathname } from '../lib/router';
import { useMe } from '../hooks/useMe';

const NAV = [
  { path: '/', key: 'nav.home', icon: '⌂' },
  { path: '/complaints/new', key: 'nav.new', icon: '＋' },
  { path: '/tickets', key: 'nav.tickets', icon: '▤' },
  { path: '/track', key: 'nav.track', icon: '◎' },
  { path: '/profile', key: 'nav.profile', icon: '◉' },
] as const;

export function Layout({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const path = usePathname();
  const { me } = useMe();
  const queryClient = useQueryClient();
  const language = (i18n.language === 'en' ? 'en' : 'ar') as AppLanguage;
  const dark = document.documentElement.classList.contains('dark');

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
    localStorage.setItem('insurance-portal:theme', next ? 'dark' : 'light');
  };

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Session may already be gone; clear local state regardless.
    }
    queryClient.clear();
    navigate('/');
    window.location.reload();
  };

  if (path.startsWith('/staff') || path.startsWith('/dashboard') || path === '/auth/staff' || path === '/auth') {
    return (
      <div className="min-h-screen bg-[#f3f5f9] text-slate-900 dark:bg-[#081a2f] dark:text-slate-100">
        <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-[radial-gradient(60%_100%_at_50%_0%,rgba(245,197,66,0.16),transparent),radial-gradient(40%_80%_at_85%_0%,rgba(16,185,129,0.14),transparent)]" />
        <header className="sticky top-0 z-40 border-b border-white/40 bg-white/70 backdrop-blur-xl dark:border-white/10 dark:bg-[#081a2f]/80">
          <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3">
            <button type="button" onClick={() => navigate('/staff')} className="flex items-center gap-2.5 text-start">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-amber-300 to-emerald-400 font-black text-slate-950 shadow">W</span>
              <span>
                <span className="block font-bold leading-tight">{t('brand')}</span>
                <span className="block text-[11px] text-slate-500 dark:text-slate-400">{t('staff.workspace')}</span>
              </span>
            </button>
            <div className="flex items-center gap-2">
              <button type="button" onClick={toggleLanguage} className="rounded-lg border border-slate-300/60 px-2.5 py-1.5 text-sm dark:border-white/15">
                {t('common.language')}
              </button>
              <button type="button" onClick={toggleTheme} aria-label={dark ? t('common.light') : t('common.dark')} className="rounded-lg border border-slate-300/60 px-2.5 py-1.5 text-sm dark:border-white/15">
                {dark ? '☀' : '☾'}
              </button>
              <button type="button" onClick={() => navigate('/')} className="hidden rounded-lg px-2.5 py-1.5 text-sm font-medium sm:block">
                {t('nav.home')}
              </button>
            </div>
          </div>
        </header>
        <main className="relative mx-auto w-full max-w-7xl px-4 pb-12 pt-6">{children}</main>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#f3f5f9] text-slate-900 dark:bg-[#081a2f] dark:text-slate-100">
      <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-[radial-gradient(60%_100%_at_50%_0%,rgba(245,197,66,0.16),transparent),radial-gradient(40%_80%_at_85%_0%,rgba(16,185,129,0.14),transparent)]" />
      <header className="sticky top-0 z-40 border-b border-white/40 bg-white/70 backdrop-blur-xl dark:border-white/10 dark:bg-[#081a2f]/80">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <button type="button" onClick={() => navigate('/')} className="flex items-center gap-2.5 text-start">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-amber-300 to-emerald-400 font-black text-slate-950 shadow">
              W
            </span>
            <span>
              <span className="block font-bold leading-tight">{t('brand')}</span>
              <span className="block text-[11px] text-slate-500 dark:text-slate-400">{t('tagline')}</span>
            </span>
          </button>
          <nav className="hidden items-center gap-1 md:flex" aria-label="primary">
            {NAV.map((item) => (
              <button
                key={item.path}
                type="button"
                onClick={() => navigate(item.path)}
                className={`rounded-lg px-3 py-2 text-sm font-medium transition ${
                  path === item.path
                    ? 'bg-amber-400/20 text-amber-600 dark:text-amber-300'
                    : 'hover:bg-slate-900/5 dark:hover:bg-white/10'
                }`}
              >
                {t(item.key)}
              </button>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            <button type="button" onClick={toggleLanguage} className="rounded-lg border border-slate-300/60 px-2.5 py-1.5 text-sm dark:border-white/15">
              {t('common.language')}
            </button>
            <button type="button" onClick={toggleTheme} aria-label={dark ? t('common.light') : t('common.dark')} className="rounded-lg border border-slate-300/60 px-2.5 py-1.5 text-sm dark:border-white/15">
              {dark ? '☀' : '☾'}
            </button>
            {me ? (
              <button type="button" onClick={() => void logout()} className="hidden rounded-lg px-2.5 py-1.5 text-sm font-medium text-rose-500 sm:block">
                {t('nav.logout')}
              </button>
            ) : (
              <button type="button" onClick={() => navigate('/login')} className="hidden rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white dark:bg-white dark:text-slate-900 sm:block">
                {t('nav.login')}
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="relative mx-auto w-full max-w-6xl px-4 pb-28 pt-6 md:pb-12">{children}</main>

      <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-white/40 bg-white/85 backdrop-blur-xl md:hidden dark:border-white/10 dark:bg-[#081a2f]/90" aria-label="mobile">
        <div className="grid grid-cols-5">
          {NAV.map((item) => (
            <button
              key={item.path}
              type="button"
              onClick={() => navigate(item.path)}
              className={`flex flex-col items-center gap-0.5 py-2 text-[10px] font-medium ${
                path === item.path ? 'text-amber-500' : 'text-slate-500 dark:text-slate-400'
              }`}
            >
              <span className="text-base leading-none">{item.icon}</span>
              {t(item.key)}
            </button>
          ))}
        </div>
      </nav>
    </div>
  );
}
