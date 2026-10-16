import { create } from 'zustand';
import { api } from '../lib/api/client';
import { ensureCsrfCookie } from '../lib/session';

export interface AccessSnapshot {
  id: string;
  email: string;
  full_name: string;
  status: string;
  token_version: number;
  two_factor_enabled: boolean;
  two_factor_required: boolean;
  roles: string[];
  permissions: string[];
  overrides: Record<string, string>;
  all_companies: boolean;
  company_ids: string[];
  insurance_types: string[];
  ownership: string | null;
  team_ids: string[];
}

interface AuthState {
  kind: 'guest' | 'customer' | 'staff';
  loading: boolean;
  customer: { id: string; full_name: string; mobile: string; email?: string | null; national_id_masked: string } | null;
  staff: AccessSnapshot | null;
  permissions: string[];
  loadMe: () => Promise<void>;
  clear: () => void;
}

export const useAuth = create<AuthState>((set, get) => ({
  kind: 'guest',
  loading: false,
  customer: null,
  staff: null,
  permissions: [],
  loadMe: async () => {
    if (!(await ensureCsrfCookie())) {
      set({ kind: 'guest', customer: null, staff: null, permissions: [], loading: false });
      return;
    }
    if (get().kind === 'guest') set({ loading: true });
    try {
      const res = await api.GET('/api/me');
      const data = res.data as unknown as ({ kind: 'staff'; access: AccessSnapshot } | ({ kind: 'customer' } & NonNullable<AuthState['customer']>) | null);
      if (res.error || !data) {
        set({ kind: 'guest', customer: null, staff: null, permissions: [], loading: false });
        return;
      }
      if (data.kind === 'staff') {
        const access = data.access as AccessSnapshot;
        set({ kind: 'staff', staff: access, permissions: access.permissions ?? [], customer: null, loading: false });
      } else {
        set({
          kind: 'customer',
          customer: data as AuthState['customer'],
          staff: null,
          permissions: [],
          loading: false,
        });
      }
    } catch {
      set({ kind: 'guest', customer: null, staff: null, permissions: [], loading: false });
    }
  },
  clear: () => set({ kind: 'guest', customer: null, staff: null, permissions: [], loading: false }),
}));
