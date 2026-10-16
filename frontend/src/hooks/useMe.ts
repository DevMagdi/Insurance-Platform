import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../lib/api';
import type { MeCustomer } from '../lib/types';

/** Current customer (401 → anonymous). Staff sessions are not portal users. */
export function useMe() {
  const query = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const me = await api.me();
      return me.kind === 'customer' ? (me as MeCustomer) : null;
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
