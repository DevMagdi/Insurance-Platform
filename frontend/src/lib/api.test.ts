import { afterEach, describe, expect, it, vi } from 'vitest';
import { api as rootApi } from '../api';
import { ApiError, api } from './api';
import { apiFetch } from './api/client';

function setCsrfCookie(): void {
  document.cookie = 'csrf_token=unit-test-csrf; Path=/; SameSite=Lax';
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('browser API authentication', () => {
  it('does not refresh the session for an invalid OTP response', async () => {
    setCsrfCookie();
    const paths: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      paths.push(String(input));
      return Response.json({ detail: 'Invalid or expired code' }, { status: 401 });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.otpVerify({ mobile: '+201123456789', code: '123456' })).rejects.toMatchObject({
      status: 401,
      detail: 'Invalid or expired code',
    } satisfies Partial<ApiError>);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(paths).toEqual(['/api/auth/customer/otp/verify']);
  });

  it('preserves Request headers and method when attaching the CSRF token', async () => {
    setCsrfCookie();
    const sentInputs: Array<RequestInfo | URL> = [];
    const requestInits: RequestInit[] = [];
    let resourceRequests = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      sentInputs.push(input);
      if (init) requestInits.push(init);
      const path = input instanceof Request
        ? new URL(input.url).pathname
        : new URL(String(input), window.location.href).pathname;
      if (path === '/api/auth/refresh') return Response.json({ refreshed: true });
      resourceRequests += 1;
      return resourceRequests === 1
        ? Response.json({ detail: 'Session expired' }, { status: 401 })
        : Response.json({ saved: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    const request = new Request(`${window.location.origin}/api/staff/complaints`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-Custom-Header': 'preserved' },
      body: '{}',
    });

    const response = await apiFetch(request);

    expect(response.ok).toBe(true);
    expect(sentInputs[0]).toBe(request);
    expect((sentInputs[0] as Request).method).toBe('PATCH');
    expect(sentInputs[2]).toBeInstanceOf(Request);
    expect(sentInputs[2]).not.toBe(request);
    expect((sentInputs[2] as Request).method).toBe('PATCH');
    expect(await (sentInputs[2] as Request).clone().text()).toBe('{}');
    expect(requestInits[0].credentials).toBe('include');
    const headers = new Headers(requestInits[2].headers);
    expect(headers.get('X-Custom-Header')).toBe('preserved');
    expect(headers.get('X-CSRF-Token')).toBe('unit-test-csrf');
  });

  it('notifies the app when a session remains unauthorized after refresh', async () => {
    setCsrfCookie();
    const requests: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), window.location.href).pathname;
      requests.push(path);
      return path === '/api/auth/refresh'
        ? Response.json({ refreshed: true })
        : Response.json({ detail: 'Session expired' }, { status: 401 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const expired = vi.fn();
    window.addEventListener('insurance:auth-expired', expired);

    try {
      await expect(api.me()).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledTimes(1);

      const response = await apiFetch('/api/me');
      expect(response.status).toBe(401);
      expect(expired).toHaveBeenCalledTimes(2);
      expect(requests.filter((path) => path === '/api/auth/refresh')).toHaveLength(2);
    } finally {
      window.removeEventListener('insurance:auth-expired', expired);
    }
  });

  it('shares one refresh across the modern and legacy clients during concurrent 401s', async () => {
    setCsrfCookie();
    const counts = new Map<string, number>();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), window.location.href).pathname;
      if (path === '/api/auth/refresh') {
        await new Promise((resolve) => setTimeout(resolve, 5));
        return Response.json({ refreshed: true });
      }

      const count = (counts.get(path) ?? 0) + 1;
      counts.set(path, count);
      if (count === 1 || (path === '/api/me' && count === 2)) {
        return Response.json({ detail: 'Session expired' }, { status: 401 });
      }
      if (path === '/api/me') {
        return Response.json({
          kind: 'customer',
          id: 'customer-1',
          full_name: 'Mona Ali',
          mobile: '+201123456789',
          email: null,
          national_id_masked: '**********2345',
        });
      }
      if (path === '/api/staff/users') return Response.json([]);
      void init;
      return Response.json({ detail: 'Unexpected request' }, { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const [me, users, openApiMe] = await Promise.all([
      api.me(),
      rootApi<unknown[]>('/api/staff/users'),
      apiFetch('/api/me').then((response) => response.json() as Promise<{ kind: string }>),
    ]);

    expect(me.kind).toBe('customer');
    expect(users).toEqual([]);
    expect(openApiMe.kind).toBe('customer');
    expect(counts.get('/api/me')).toBe(4);
    expect(counts.get('/api/staff/users')).toBe(2);
    const refreshCalls = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/auth/refresh');
    expect(refreshCalls).toHaveLength(1);
    expect(new Headers(refreshCalls[0][1]?.headers).get('X-CSRF-Token')).toBe('unit-test-csrf');
  });
});
