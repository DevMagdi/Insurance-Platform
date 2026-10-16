import { afterEach, describe, expect, it, vi } from 'vitest';
import { ensureCsrfCookie, getCsrfToken, shouldAttemptRefresh } from './session';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('browser session cookie handling', () => {
  it('shares one CSRF-cookie migration request and observes the migrated root cookie', async () => {
    const requests: Array<{ url: string; credentials?: RequestCredentials }> = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), credentials: init?.credentials });
      document.cookie = 'csrf_token=migrated; Path=/; SameSite=Lax';
      return Promise.resolve({ ok: true } as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    const results = await Promise.all([ensureCsrfCookie(), ensureCsrfCookie()]);

    expect(results).toEqual([true, true]);
    expect(requests).toEqual([{ url: '/api/auth/csrf', credentials: 'same-origin' }]);
  });

  it('only reads the CSRF cookie when it is visible from the SPA root', () => {
    document.cookie = 'csrf_token=legacy; Path=/api; SameSite=Lax';
    expect(getCsrfToken()).toBeNull();

    document.cookie = 'csrf_token=browser-readable; Path=/; SameSite=Lax';
    expect(getCsrfToken()).toBe('browser-readable');
  });

  it('refreshes only authenticated same-origin requests, not public or auth challenges', () => {
    expect(shouldAttemptRefresh('/api/me')).toBe(false);
    document.cookie = 'csrf_token=session-csrf; Path=/; SameSite=Lax';

    expect(shouldAttemptRefresh('/api/me')).toBe(true);
    expect(shouldAttemptRefresh('/api/staff/complaints')).toBe(true);
    expect(shouldAttemptRefresh('/api/auth/logout')).toBe(true);
    expect(shouldAttemptRefresh('/api/auth/customer/otp/verify')).toBe(false);
    expect(shouldAttemptRefresh('/api/auth/staff/login')).toBe(false);
    expect(shouldAttemptRefresh('/api/guest/track')).toBe(false);
    expect(shouldAttemptRefresh('/api/companies')).toBe(false);
    expect(shouldAttemptRefresh('https://other.example/api/me')).toBe(false);
  });
});
