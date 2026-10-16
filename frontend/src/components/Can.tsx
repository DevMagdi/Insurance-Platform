import type { ReactNode } from 'react';
import { usePermission } from '../hooks/usePermission';

interface CanProps {
  permission?: string;
  permissions?: string[];
  mode?: 'any' | 'all';
  children: ReactNode;
  fallback?: ReactNode;
}

export function Can({ permission, permissions, mode = 'any', children, fallback = null }: CanProps) {
  const { has, hasAny, hasAll } = usePermission();
  let allowed = true;
  if (permission) allowed = has(permission);
  if (permissions && permissions.length) {
    allowed = mode === 'all' ? hasAll(permissions) : hasAny(permissions);
  }
  return allowed ? <>{children}</> : <>{fallback}</>;
}
