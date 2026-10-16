import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../lib/api';
import type { StaffAccess } from '../lib/staffTypes';

/** Current staff principal and its effective DB-derived access snapshot. */
export function useStaffMe() {
  const query = useQuery({
    queryKey: ['staff-me'],
    queryFn: async () => {
      const me = await api.me();
      return me.kind === 'staff' ? me.access : null;
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
