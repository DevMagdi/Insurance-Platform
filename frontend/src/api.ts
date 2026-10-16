import { getCsrfToken, notifyAuthExpired, refreshSession, shouldAttemptRefresh } from './lib/session';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

function assertSameOriginApiPath(path: string): void {
  if (!path.startsWith('/api/') || path.startsWith('//') || /^https?:/i.test(path)) {
    throw new Error('Browser API calls must use a same-origin /api path');
  }
}

function makeHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (init.body && typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const method = (init.method ?? 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = getCsrfToken();
    if (token) headers.set('X-CSRF-Token', token);
  }
  return headers;
}

async function send(path: string, init: RequestInit, allowRefresh: boolean): Promise<Response> {
  assertSameOriginApiPath(path);
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: makeHeaders(init),
  });
  if (response.status === 401 && shouldAttemptRefresh(path)) {
    if (allowRefresh && await refreshSession()) return send(path, init, false);
    notifyAuthExpired();
  }
  if (response.status === 403) {
    window.dispatchEvent(new CustomEvent('insurance:permission-denied'));
  }
  return response;
}

async function errorFor(response: Response): Promise<ApiError> {
  let detail = `Request failed (${response.status})`;
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const value = (body as { detail?: unknown }).detail;
      if (typeof value === 'string') detail = value;
    }
  } catch {
    // Keep the status-based message for non-JSON provider/proxy failures.
  }
  return new ApiError(response.status, detail);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await send(path, init, true);
  if (!response.ok) throw await errorFor(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiResponse(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await send(path, init, true);
  if (!response.ok) throw await errorFor(response);
  return response;
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export function queryString(values: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}
