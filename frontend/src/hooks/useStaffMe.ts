import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../lib/api';
import { ensureCsrfCookie } from '../lib/session';
import type { StaffAccess } from '../lib/staffTypes';

/** Current staff principal and its effective DB-derived access snapshot. */
export function useStaffMe() {
  const query = useQuery({
    queryKey: ['staff-me'],
    queryFn: async () => {
      if (!(await ensureCsrfCookie())) return null;
      try {
        const me = await api.me();
        return me.kind === 'staff' ? me.access : null;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: 30_000,
  });
  const anonymous = query.error instanceof ApiError && query.error.status === 401;
  const access: StaffAccess | null = query.data ?? null;
  return {
    access,
    isLoading: query.isLoading && !anonymous,
    isStaff: Boolean(access),
    refetch: query.refetch,
  };
}
