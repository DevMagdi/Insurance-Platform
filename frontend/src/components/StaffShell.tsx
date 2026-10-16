import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { navigate, usePathname } from '../lib/router';
import { hasPermission } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { EmptyState, LoadingState, primaryButton } from './ui';

const LINKS = [
  { path: '/staff', key: 'staff.queue', permission: 'complaints.view' },
  { path: '/staff/analytics', key: 'staff.analytics', permission: 'analytics.view' },
  { path: '/staff/admin', key: 'staff.admin', permission: 'users.view' },
  { path: '/staff/audit', key: 'staff.audit', permission: 'audit.view' },
  { path: '/staff/exports', key: 'staff.exports', permission: 'complaints.export' },
] as const;

export function StaffShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const path = usePathname();
  const queryClient = useQueryClient();
  const { access, isLoading } = useStaffMe();

  if (isLoading) return <LoadingState />;
  if (!access) {
    return (
      <section className="mx-auto max-w-xl rounded-3xl border border-white/40 bg-white/70 p-6 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h1 className="text-2xl font-black">{t('staff.workspace')}</h1>
        <p className="mt-2 text-slate-500 dark:text-slate-300">{t('staff.noAccess')}</p>
        <button type="button" className={`${primaryButton} mt-5`} onClick={() => navigate('/staff/login')}>
          {t('staff.loginTitle')}
        </button>
      </section>
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
    <div className="space-y-5">
      <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs font-bold uppercase tracking-widest text-amber-600 dark:text-amber-300">
              {t('staff.workspace')}
            </p>
            <h1 className="mt-1 text-2xl font-black">{access.full_name}</h1>
            <p className="text-sm text-slate-500 dark:text-slate-300">{access.email}</p>
          </div>
          <button type="button" onClick={() => void logout()} className="rounded-xl px-3 py-2 text-sm font-semibold text-rose-500">
            {t('nav.logout')}
          </button>
        </div>
        <nav className="mt-4 flex flex-wrap gap-2" aria-label="staff">
          {LINKS.filter((link) => hasPermission(access, link.permission)).map((link) => (
            <button
              key={link.path}
              type="button"
              onClick={() => navigate(link.path)}
              className={`rounded-full px-4 py-2 text-sm font-semibold transition ${
                path === link.path
                  ? 'bg-slate-950 text-white dark:bg-white dark:text-slate-950'
                  : 'bg-slate-900/5 hover:bg-slate-900/10 dark:bg-white/10 dark:hover:bg-white/15'
              }`}
            >
              {t(link.key)}
            </button>
          ))}
        </nav>
      </section>
      {children ?? <EmptyState message={t('staff.noAccess')} />}
    </div>
  );
}
