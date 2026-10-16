import type { StaffAccess } from '../types';

export function hasPermission(access: StaffAccess, code: string): boolean {
  return access.permissions.includes(code as (typeof access.permissions)[number]);
}
