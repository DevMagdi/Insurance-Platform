import { useEffect } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CAPTCHA_HEADER, captchaHeaders, resetCaptchaCache, useCaptcha, type CaptchaState } from './captcha';

const sink: { current: CaptchaState | null } = { current: null };

function Probe({ action }: { action: string }) {
  const state = useCaptcha(action);
  useEffect(() => {
    sink.current = state;
  });
  return <div>{state.widget}<span data-testid="ready">{String(state.ready)}</span></div>;
}

afterEach(() => {
  resetCaptchaCache();
  delete window.turnstile;
  sink.current = null;
  vi.unstubAllGlobals();
});

describe('Turnstile CAPTCHA', () => {
  it('is inert when the server reports no CAPTCHA provider', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ captcha: null, privacy_policy_version: '2026-10' })));
    render(<Probe action="otp_start" />);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/public/config', expect.anything()));
    expect(screen.queryByTestId('captcha-widget')).toBeNull();
    expect(screen.getByTestId('ready').textContent).toBe('true');
    expect(captchaHeaders(null)).toEqual({});
  });

  it('treats a missing config endpoint as no CAPTCHA (server still enforces)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404 })));
    render(<Probe action="guest_track" />);
    await waitFor(() => expect(sink.current?.required).toBe(false));
  });

  it('renders the widget with the bound action and exposes single-use tokens', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ captcha: { provider: 'turnstile', site_key: 'site-1' }, privacy_policy_version: 'v' })));
    let callback: ((token: string) => void) | undefined;
    const turnstile = {
      render: vi.fn((_el: HTMLElement, options: { sitekey: string; action: string; callback: (token: string) => void }) => {
        callback = options.callback;
        return 'widget-1';
      }),
      reset: vi.fn(),
      remove: vi.fn(),
    };
    window.turnstile = turnstile;
    const { unmount } = render(<Probe action="staff_login" />);
    await waitFor(() => expect(turnstile.render).toHaveBeenCalled());
    expect(turnstile.render.mock.calls[0][1]).toMatchObject({ sitekey: 'site-1', action: 'staff_login' });
    expect(screen.getByTestId('ready').textContent).toBe('false');
    act(() => callback?.('token-abc'));
    expect(sink.current?.token).toBe('token-abc');
    expect(captchaHeaders(sink.current?.token)).toEqual({ [CAPTCHA_HEADER]: 'token-abc' });
    act(() => sink.current?.reset());
    expect(sink.current?.token).toBeNull();
    expect(turnstile.reset).toHaveBeenCalledWith('widget-1');
    unmount();
    expect(turnstile.remove).toHaveBeenCalledWith('widget-1');
  });
});
