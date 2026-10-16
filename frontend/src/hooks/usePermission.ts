import { useAuth } from '../store/auth';

export function usePermission() {
  const permissions = useAuth((s) => s.permissions);
  const kind = useAuth((s) => s.kind);
  const has = (perm: string) => kind === 'staff' && permissions.includes(perm);
  const hasAny = (perms: string[]) => perms.some(has);
  const hasAll = (perms: string[]) => perms.every(has);
  return { permissions, has, hasAny, hasAll, isStaff: kind === 'staff', isCustomer: kind === 'customer' };
}
