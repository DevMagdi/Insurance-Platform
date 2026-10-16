import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { resetCaptchaCache } from '../lib/captcha';
import { LoginPage } from './LoginPage';

function renderLoginPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

describe('customer OTP registration flow', () => {
  it('keeps the verified OTP available while collecting first-time registration details', async () => {
    resetCaptchaCache();
    const verifyBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/public/config') {
        return Response.json({ captcha: null, privacy_policy_version: '2026-10' });
      }
      if (path === '/api/auth/customer/otp/start') {
        return Response.json({ message: 'sent' }, { status: 202 });
      }
      if (path === '/api/auth/customer/otp/verify') {
        verifyBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        if (verifyBodies.length === 1) {
          return Response.json({ detail: 'Provide national ID and name to register' }, { status: 409 });
        }
        return Response.json({ authenticated_as: 'customer' });
      }
      return Response.json({ detail: 'Unexpected request' }, { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    renderLoginPage();

    fireEvent.change(screen.getByLabelText('رقم الجوال'), { target: { value: '01123456789' } });
    fireEvent.click(screen.getByRole('button', { name: 'إرسال الرمز' }));
    expect(await screen.findByText(/تم إرسال الرمز/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText('رمز التحقق'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'تأكيد ودخول' }));
    expect(await screen.findByText(/للتأكد من هويتك/)).toBeTruthy();
    expect((screen.getByLabelText('رمز التحقق') as HTMLInputElement).value).toBe('123456');

    fireEvent.change(screen.getByLabelText('الاسم الكامل'), { target: { value: 'Mona Ali' } });
    fireEvent.change(screen.getByLabelText(/الرقم القومي/), { target: { value: '29901010112345' } });
    fireEvent.click(screen.getByRole('button', { name: 'تأكيد ودخول' }));

    await waitFor(() => expect(verifyBodies).toHaveLength(2));
    expect(verifyBodies[0]).toEqual({ mobile: '+201123456789', code: '123456' });
    expect(verifyBodies[1]).toMatchObject({
      mobile: '+201123456789',
      code: '123456',
      full_name: 'Mona Ali',
      national_id: '29901010112345',
      whatsapp_available: false,
    });
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/auth/refresh')).toBe(false);
    expect(window.location.pathname).toBe('/tickets');
  });
});
