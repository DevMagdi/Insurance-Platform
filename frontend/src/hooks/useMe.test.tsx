import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useMe } from './useMe';

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('useMe session bootstrap', () => {
  it('checks for a legacy CSRF cookie but skips /api/me for anonymous visitors', async () => {
    const requestUrls: string[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      requestUrls.push(String(input));
      return Promise.resolve({ ok: true } as Response);
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useMe(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.me).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(requestUrls).toEqual(['/api/auth/csrf']);
  });
});
