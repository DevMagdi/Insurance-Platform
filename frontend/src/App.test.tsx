import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

function renderApp(path = '/') {
  window.history.pushState({}, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

describe('Phase 6 customer portal shell', () => {
  it('renders the Arabic-first landing with complaint and tracking CTAs', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{"detail":"unauthorized"}', { status: 401 })),
    );
    renderApp('/');
    expect(screen.getByRole('heading', { name: /شكواك تستحق متابعة/ })).toBeTruthy();
    expect(screen.getByText('قدّم شكوى جديدة')).toBeTruthy();
    expect(document.documentElement.dir === 'rtl' || document.documentElement.lang === 'ar').toBe(true);
  });

  it('switches the whole UI to English LTR', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{"detail":"unauthorized"}', { status: 401 })),
    );
    renderApp('/');
    fireEvent.click(screen.getByText('English'));
    await waitFor(() => expect(screen.getByText('Your complaint deserves VIP follow-up')).toBeTruthy());
    expect(document.documentElement.dir).toBe('ltr');
    expect(document.documentElement.lang).toBe('en');
  });

  it('validates the guest tracking form before calling the API', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => {
      void _input;
      return new Response('{"detail":"unauthorized"}', { status: 401 });
    });
    vi.stubGlobal('fetch', fetchMock);
    renderApp('/track');
    fireEvent.click(screen.getByText('تتبع الشكوى'));
    await waitFor(() => expect(screen.getByText(/رقم التذكرة بصيغة/)).toBeTruthy());
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/guest/track'))).toBe(false);
  });
});
