import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../lib/api';
import { ensureCsrfCookie } from '../lib/session';
import type { MeCustomer } from '../lib/types';

/** Current customer (401 → anonymous). Staff sessions are not portal users. */
export function useMe() {
  const query = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      // Migrate older API-scoped CSRF cookies, then avoid an expected anonymous
      // /api/me 401 on every public page when no session exists.
      if (!(await ensureCsrfCookie())) return null;
      try {
        const me = await api.me();
        return me.kind === 'customer' ? (me as MeCustomer) : null;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: 60_000,
  });
  const anonymous = query.error instanceof ApiError && query.error.status === 401;
  return {
    me: query.data ?? null,
    isLoading: query.isLoading && !anonymous,
    isAuthenticated: Boolean(query.data),
    refetch: query.refetch,
  };
}
