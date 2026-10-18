import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ApiError, staffApi } from '../lib/api';
import { accessSpecFromSnapshot, catalogSpec, hasPermission } from '../lib/staffTypes';
import { useStaffMe } from '../hooks/useStaffMe';
import { StaffShell } from '../components/StaffShell';
import { EmptyState, ErrorState, Field, LoadingState, SelectInput, TextArea, TextInput, Toggle, ghostButton, primaryButton } from '../components/ui';

type CatalogKind = 'companies' | 'categories' | 'sla-rules' | 'templates';

export function StaffAdminPage() {
  const { t } = useTranslation();
  const { access } = useStaffMe();
  const queryClient = useQueryClient();
  const [error, setError] = useState('');
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteName, setInviteName] = useState('');
  const [inviteDepartment, setInviteDepartment] = useState('');
  const [selectedUser, setSelectedUser] = useState('');
  const [accessJson, setAccessJson] = useState('{\n  "role_ids": [],\n  "allow": [],\n  "deny": [],\n  "all_companies": true,\n  "company_ids": [],\n  "insurance_types": [],\n  "ownership": "all",\n  "team_ids": []\n}');
  const [roleId, setRoleId] = useState<string | null>(null);
  const [roleName, setRoleName] = useState('');
  const [roleDescription, setRoleDescription] = useState('');
  const [rolePermissions, setRolePermissions] = useState('');
  const [catalogKind, setCatalogKind] = useState<CatalogKind>('companies');
  const [catalogId, setCatalogId] = useState<string | null>(null);
  const [catalogJson, setCatalogJson] = useState('{}');
  const [global2fa, setGlobal2fa] = useState(false);

  const departments = useQuery({ queryKey: ['staff-departments'], queryFn: staffApi.departments, enabled: hasPermission(access, 'users.view') });
  const users = useQuery({ queryKey: ['staff-users'], queryFn: staffApi.users, enabled: hasPermission(access, 'users.view') });
  const roles = useQuery({ queryKey: ['staff-roles'], queryFn: staffApi.roles, enabled: hasPermission(access, 'roles.manage') });
  const catalog = useQuery({
    queryKey: ['staff-catalog', catalogKind],
    queryFn: () => staffApi.catalog(catalogKind),
    enabled:
      (catalogKind === 'companies' && hasPermission(access, 'companies.manage')) ||
      (catalogKind === 'categories' && hasPermission(access, 'categories.manage')) ||
      (catalogKind === 'sla-rules' && hasPermission(access, 'sla.manage')) ||
      (catalogKind === 'templates' && hasPermission(access, 'templates.manage')),
  });

  const act = useMutation({
    mutationFn: async (work: () => Promise<unknown>) => work(),
    onError: (caught) => setError(caught instanceof ApiError ? caught.detail : t('errors.generic')),
    onSuccess: async () => {
      setError('');
      await queryClient.invalidateQueries({ queryKey: ['staff-users'] });
      await queryClient.invalidateQueries({ queryKey: ['staff-roles'] });
      await queryClient.invalidateQueries({ queryKey: ['staff-catalog'] });
    },
  });

  const loadAccess = async (id: string) => {
    setSelectedUser(id);
    try {
      const snapshot = await staffApi.userAccess(id);
      setAccessJson(JSON.stringify(accessSpecFromSnapshot(snapshot), null, 2));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : t('errors.generic'));
    }
  };

  return (
    <StaffShell>
      {error && <ErrorState message={error} />}
      {!hasPermission(access, 'users.view') && !hasPermission(access, 'roles.manage') ? (
        <EmptyState message={t('staff.noAccess')} />
      ) : (
        <div className="space-y-5">
          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h2 className="text-xl font-black">{t('staff.admin')} · {t('staff.invite')}</h2>
            <div className="mt-4 grid gap-3 md:grid-cols-3">
              <Field label={t('staff.email')}><TextInput type="email" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} /></Field>
              <Field label={t('staff.fullName')}><TextInput value={inviteName} onChange={(e) => setInviteName(e.target.value)} /></Field>
              <Field label="القسم"><SelectInput required value={inviteDepartment} onChange={(event) => setInviteDepartment(event.target.value)}><option value="">اختر القسم</option>{departments.data?.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}</SelectInput></Field>
              <div className="flex items-end">
                <button type="button" className={primaryButton} disabled={!inviteEmail || !inviteDepartment || inviteName.trim().length < 2 || act.isPending} onClick={() => act.mutate(() => staffApi.inviteUser(inviteEmail, inviteName, inviteDepartment))}>
                  {t('staff.invite')}
                </button>
              </div>
            </div>
            <div className="mt-4 max-w-sm">
              <Toggle checked={global2fa} onChange={(value) => { setGlobal2fa(value); act.mutate(() => staffApi.globalTotpPolicy(value)); }} label={t('staff.require2fa')} />
            </div>
          </section>

          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('staff.admin')}</h3>
            {users.isLoading ? <LoadingState /> : users.isError ? <ErrorState message={t('staff.loadError')} /> : (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[720px] text-sm">
                  <thead><tr className="border-b text-start text-slate-500 dark:border-white/10"><th className="py-2">{t('staff.fullName')}</th><th>{t('staff.email')}</th><th>{t('staff.status')}</th><th>2FA</th><th>{t('staff.actions')}</th></tr></thead>
                  <tbody>
                    {(users.data ?? []).map((user) => (
                      <tr key={user.id} className="border-b border-slate-100 dark:border-white/5">
                        <td className="py-2 font-bold">{user.full_name}</td><td>{user.email}</td><td>{user.status}</td><td>{user.two_factor_enabled ? '✓' : '—'}</td>
                        <td>
                          <div className="flex flex-wrap gap-2 py-2">
                            <button type="button" className="font-bold text-amber-600 dark:text-amber-300" onClick={() => void loadAccess(user.id)}>{t('staff.access')}</button>
                            <button type="button" className="font-bold" onClick={() => act.mutate(() => staffApi.resetUserPassword(user.id))}>{t('staff.resetPassword')}</button>
                            <button type="button" className="font-bold" onClick={() => act.mutate(() => staffApi.forceLogoutUser(user.id))}>{t('staff.forceLogout')}</button>
                            {user.status === 'disabled' ? (
                              <button type="button" className="font-bold text-emerald-600" onClick={() => act.mutate(() => staffApi.reactivateUser(user.id))}>{t('staff.reactivate')}</button>
                            ) : (
                              <button type="button" className="font-bold text-rose-500" onClick={() => act.mutate(() => staffApi.deactivateUser(user.id))}>{t('staff.deactivate')}</button>
                            )}
                            <button type="button" className="font-bold" onClick={() => act.mutate(() => staffApi.userTotpPolicy(user.id, !user.two_factor_required))}>{t('staff.require2fa')}</button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {selectedUser && (
              <div className="mt-5">
                <Field label={`${t('staff.access')} · ${selectedUser}`}>
                  <TextArea className="min-h-48 font-mono text-xs" dir="ltr" value={accessJson} onChange={(e) => setAccessJson(e.target.value)} />
                </Field>
                <button type="button" className={`${primaryButton} mt-3`} disabled={act.isPending} onClick={() => act.mutate(() => staffApi.replaceAccess(selectedUser, JSON.parse(accessJson) as Record<string, unknown>))}>
                  {t('common.save')}
                </button>
              </div>
            )}
          </section>

          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <h3 className="font-black">{t('staff.roles')}</h3>
            <div className="mt-3 flex flex-wrap gap-2">
              {(roles.data ?? []).map((role) => (
                <button key={role.id} type="button" className="rounded-full bg-slate-950/5 px-3 py-1.5 text-sm font-bold dark:bg-white/10" onClick={() => { setRoleId(role.id); setRoleName(role.name); setRoleDescription(role.description); setRolePermissions(role.permissions.join(', ')); }}>
                  {role.name} · {role.permissions.length}
                </button>
              ))}
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-3">
              <Field label="Name"><TextInput value={roleName} onChange={(e) => setRoleName(e.target.value)} /></Field>
              <Field label="Description"><TextInput value={roleDescription} onChange={(e) => setRoleDescription(e.target.value)} /></Field>
              <Field label="Permissions (comma-separated)"><TextInput dir="ltr" value={rolePermissions} onChange={(e) => setRolePermissions(e.target.value)} /></Field>
            </div>
            <button type="button" className={`${primaryButton} mt-3`} disabled={roleName.trim().length < 2 || act.isPending} onClick={() => act.mutate(() => staffApi.saveRole(roleId, { name: roleName, description: roleDescription, permissions: rolePermissions.split(',').map((x) => x.trim()).filter(Boolean) }))}>
              {t('common.save')}
            </button>
          </section>

          <section className="rounded-3xl border border-white/40 bg-white/70 p-5 shadow-xl backdrop-blur dark:border-white/10 dark:bg-white/5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="font-black">{t('staff.catalog')}</h3>
              <SelectInput className="max-w-52" value={catalogKind} onChange={(e) => { setCatalogKind(e.target.value as CatalogKind); setCatalogId(null); setCatalogJson('{}'); }}>
                <option value="companies">companies</option><option value="categories">categories</option><option value="sla-rules">sla-rules</option><option value="templates">templates</option>
              </SelectInput>
            </div>
            {catalog.isLoading ? <LoadingState /> : catalog.isError ? <ErrorState message={t('staff.loadError')} /> : (
              <>
                <div className="mt-3 flex flex-wrap gap-2">
                  {(catalog.data ?? []).slice(0, 30).map((record) => (
                    <button key={String(record.id)} type="button" className="rounded-full bg-slate-950/5 px-3 py-1.5 text-xs font-bold dark:bg-white/10" onClick={() => { setCatalogId(String(record.id)); setCatalogJson(JSON.stringify(catalogSpec(catalogKind, record), null, 2)); }}>
                      {String(record.name_en ?? record.title ?? record.id).slice(0, 36)}
                    </button>
                  ))}
                </div>
                <Field label={catalogKind}><TextArea className="mt-4 min-h-48 font-mono text-xs" dir="ltr" value={catalogJson} onChange={(e) => setCatalogJson(e.target.value)} /></Field>
                <button type="button" className={`${primaryButton} mt-3`} disabled={act.isPending} onClick={() => act.mutate(() => staffApi.saveCatalog(catalogKind, catalogId, catalogSpec(catalogKind, JSON.parse(catalogJson) as Record<string, unknown>)))}>
                  {t('common.save')}
                </button>
                <button type="button" className={`${ghostButton} mt-3 ms-2`} onClick={() => { setCatalogId(null); setCatalogJson('{}'); }}>New</button>
              </>
            )}
          </section>
        </div>
      )}
    </StaffShell>
  );
}
