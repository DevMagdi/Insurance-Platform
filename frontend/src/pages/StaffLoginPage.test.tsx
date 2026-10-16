import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import '../i18n';
import { StaffLoginPage } from './StaffLoginPage';

describe('StaffLoginPage', () => {
  it('routes a TOTP challenge to the code step without exposing a session', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/auth/staff/login')) {
        return Response.json({ next_step: 'totp_verify', challenge: 'challenge-token' });
      }
      return new Response('{}', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<StaffLoginPage />);
    fireEvent.change(screen.getByLabelText(/بريد العمل/), { target: { value: 'agent@example.test' } });
    fireEvent.change(screen.getByLabelText(/كلمة المرور/), { target: { value: 'correct-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'تسجيل الدخول' }));
    await waitFor(() => expect(screen.getByLabelText(/رمز المصادقة/)).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/staff/login',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('starts authenticator enrollment when setup is required', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/auth/staff/login')) {
        return Response.json({ next_step: 'totp_setup', challenge: 'challenge-token' });
      }
      if (url.includes('/api/auth/staff/2fa/enroll/start')) {
        return Response.json({ provisioning_uri: 'otpauth://totp/test?secret=abc' });
      }
      return new Response('{}', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<StaffLoginPage />);
    fireEvent.change(screen.getByLabelText(/بريد العمل/), { target: { value: 'agent@example.test' } });
    fireEvent.change(screen.getByLabelText(/كلمة المرور/), { target: { value: 'correct-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'تسجيل الدخول' }));
    await waitFor(() => expect(screen.getByText(/otpauth:\/\/totp\/test/)).toBeTruthy());
  });
});
