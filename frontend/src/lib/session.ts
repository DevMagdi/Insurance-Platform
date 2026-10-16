/** Shared CSRF and refresh handling for all browser API clients. */
let csrfBootstrapPromise: Promise<boolean> | null = null;
let refreshPromise: Promise<boolean> | null = null;

export function getCsrfToken(): string | null {
  if (typeof document === 'undefined') return null;

  const value = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith('csrf_token='))
    ?.slice('csrf_token='.length);

  if (value === undefined) return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

function requestPath(input: string | URL | Request): string | null {
  const value = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  try {
    const url = new URL(value, window.location.href);
    return url.origin === window.location.origin ? url.pathname : null;
  } catch {
    return null;
  }
}

/** Migrate the old API-scoped CSRF cookie without treating anonymous visitors as signed in. */
export function ensureCsrfCookie(): Promise<boolean> {
  if (getCsrfToken()) return Promise.resolve(true);
  if (typeof window === 'undefined') return Promise.resolve(false);

  if (!csrfBootstrapPromise) {
    csrfBootstrapPromise = fetch('/api/auth/csrf', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then((response) => response.ok && Boolean(getCsrfToken()))
      .catch(() => false)
      .finally(() => {
        csrfBootstrapPromise = null;
      });
  }
  return csrfBootstrapPromise;
}

/**
 * Only retry requests that use the browser's cookie session. Invalid credentials,
 * OTPs, CAPTCHA challenges and public endpoints must keep their original 401.
 */
export function shouldAttemptRefresh(input: string | URL | Request): boolean {
  if (!getCsrfToken()) return false;

  const path = requestPath(input);
  if (!path?.startsWith('/api/')) return false;
  if (path === '/api/auth/logout') return true;
  if (path.startsWith('/api/auth/')) return false;

  return !(
    path === '/api/companies' ||
    path === '/api/demo-requests' ||
    path.startsWith('/api/guest/') ||
    path.startsWith('/api/public/') ||
    path.startsWith('/api/webhooks/') ||
    path.startsWith('/api/health/') ||
    path === '/api/metrics' ||
    path === '/api/docs' ||
    path === '/api/redoc' ||
    path === '/api/openapi.json'
  );
}

/** All clients share one refresh to avoid refresh-token rotation/replay races. */
export function refreshSession(): Promise<boolean> {
  const csrf = getCsrfToken();
  if (!csrf) return Promise.resolve(false);

  if (!refreshPromise) {
    refreshPromise = fetch('/api/auth/refresh', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'X-CSRF-Token': csrf },
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

export function notifyAuthExpired(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('insurance:auth-expired'));
  }
}
