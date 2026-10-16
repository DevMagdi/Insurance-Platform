import createClient from 'openapi-fetch';
import type { paths } from './schema';
import {
  getCsrfToken,
  notifyAuthExpired,
  refreshSession,
  shouldAttemptRefresh,
} from '../session';

export const API_BASE = '';

const baseFetch: typeof fetch = async (input, init) => {
  const request = input instanceof Request ? input : null;
  const method = (init?.method ?? request?.method ?? 'GET').toUpperCase();
  const headers = new Headers(request?.headers);
  new Headers(init?.headers).forEach((value, name) => headers.set(name, value));
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = getCsrfToken();
    if (csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf);
  }

  const retryInput = request?.clone() ?? input;
  const requestInit: RequestInit = { ...init, headers, credentials: 'include' };
  const response = await fetch(input, requestInit);
  if (response.status !== 401 || !shouldAttemptRefresh(input)) return response;

  if (!(await refreshSession())) {
    notifyAuthExpired();
    return response;
  }

  const retryHeaders = new Headers(headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = getCsrfToken();
    if (csrf) retryHeaders.set('X-CSRF-Token', csrf);
  }
  const retryResponse = await fetch(retryInput, { ...init, headers: retryHeaders, credentials: 'include' });
  if (retryResponse.status === 401) notifyAuthExpired();
  return retryResponse;
};

export const api = createClient<paths>({ baseUrl: API_BASE, fetch: baseFetch });

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit) {
  return baseFetch(input, init);
}
