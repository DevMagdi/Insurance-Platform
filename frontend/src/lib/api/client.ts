import createClient from 'openapi-fetch';
import type { paths } from './schema';
import { getCookie } from '../utils';

export const API_BASE = '';

let refreshPromise: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  try {
    const csrf = getCookie('csrf_token');
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
    });
    return res.ok;
  } catch {
    return false;
  }
}

function ensureRefresh() {
  if (!refreshPromise) {
    refreshPromise = doRefresh().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

const baseFetch: typeof fetch = async (input, init) => {
  const method = (init?.method ?? 'GET').toUpperCase();
  const headers = new Headers(init?.headers);
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const csrf = getCookie('csrf_token');
    if (csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf);
  }
  const res = await fetch(input, { ...init, headers, credentials: 'include' });
  if (res.status === 401 && !String(input).includes('/api/auth/refresh')) {
    const ok = await ensureRefresh();
    if (ok) {
      const retryHeaders = new Headers(init?.headers);
      if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
        const csrf = getCookie('csrf_token');
        if (csrf) retryHeaders.set('X-CSRF-Token', csrf);
      }
      return fetch(input, { ...init, headers: retryHeaders, credentials: 'include' });
    }
  }
  return res;
};

export const api = createClient<paths>({ baseUrl: API_BASE, fetch: baseFetch });

export async function apiFetch(path: string, init?: RequestInit) {
  return baseFetch(path, init);
}
