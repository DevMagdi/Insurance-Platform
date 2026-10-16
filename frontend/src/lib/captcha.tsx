/**
 * Cloudflare Turnstile integration for public/abuse-prone forms (OTP start,
 * guest tracking, staff login, invitation acceptance).
 *
 * The site key comes from `/api/public/config` at runtime, so one build works
 * in every environment. Tokens are single-use: call `reset()` after every
 * request. When the backend reports no CAPTCHA provider (development, tests)
 * the widget renders nothing and requests carry no token; the server remains
 * the enforcement point either way.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

export interface CaptchaConfig {
  provider: 'turnstile';
  site_key: string;
}

export interface PublicConfig {
  captcha: CaptchaConfig | null;
  privacy_policy_version: string;
}

interface TurnstileOptions {
  sitekey: string;
  action: string;
  callback: (token: string) => void;
  'expired-callback': () => void;
  'error-callback': () => void;
  theme?: 'auto' | 'light' | 'dark';
  language?: string;
  size?: 'normal' | 'flexible' | 'compact';
}

interface TurnstileApi {
  render(element: HTMLElement, options: TurnstileOptions): string;
  reset(widgetId?: string): void;
  remove(widgetId: string): void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

export const CAPTCHA_HEADER = 'X-Captcha-Token';
const SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
const FALLBACK: PublicConfig = { captcha: null, privacy_policy_version: 'unknown' };

let configPromise: Promise<PublicConfig> | null = null;
let scriptPromise: Promise<TurnstileApi> | null = null;

export function loadPublicConfig(): Promise<PublicConfig> {
  configPromise ??= fetch('/api/public/config', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    .then(async (response) => (response.ok ? ((await response.json()) as PublicConfig) : FALLBACK))
    .catch(() => FALLBACK);
  return configPromise;
}

function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  scriptPromise ??= new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = SCRIPT_URL;
    script.async = true;
    script.defer = true;
    script.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error('turnstile unavailable')));
    script.onerror = () => {
      scriptPromise = null;
      reject(new Error('turnstile failed to load'));
    };
    document.head.appendChild(script);
  });
  return scriptPromise;
}

/** Test hook: forget cached config/script between test cases. */
export function resetCaptchaCache(): void {
  configPromise = null;
  scriptPromise = null;
}

export function captchaHeaders(token: string | null | undefined): Record<string, string> {
  return token ? { [CAPTCHA_HEADER]: token } : {};
}

export interface CaptchaState {
  /** Element to place inside the form (null when CAPTCHA is disabled). */
  widget: ReactNode;
  token: string | null;
  /** True when the server requires a token for this action. */
  required: boolean;
  failed: boolean;
  /** Tokens are single-use: reset after each submission attempt. */
  reset: () => void;
  /** True when the request may be sent (no CAPTCHA, or a token is present). */
  ready: boolean;
}

export function useCaptcha(action: string): CaptchaState {
  const [config, setConfig] = useState<CaptchaConfig | null | undefined>(undefined);
  const [token, setToken] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // Callback ref: forms that unmount/remount (multi-step flows) re-render the widget.
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const widgetId = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    void loadPublicConfig().then((value) => {
      if (active) setConfig(value.captcha);
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!config || !element) return undefined;
    let cancelled = false;
    loadTurnstile()
      .then((turnstile) => {
        if (cancelled) return;
        widgetId.current = turnstile.render(element, {
          sitekey: config.site_key,
          action,
          callback: (value) => {
            setFailed(false);
            setToken(value);
          },
          'expired-callback': () => setToken(null),
          'error-callback': () => {
            setToken(null);
            setFailed(true);
          },
          theme: 'auto',
          language: document.documentElement.lang || 'auto',
          size: 'flexible',
        });
      })
      .catch(() => setFailed(true));
    return () => {
      cancelled = true;
      if (widgetId.current && window.turnstile) window.turnstile.remove(widgetId.current);
      widgetId.current = null;
    };
  }, [config, action, element]);

  const reset = useCallback(() => {
    setToken(null);
    if (widgetId.current && window.turnstile) window.turnstile.reset(widgetId.current);
  }, []);

  const widget = config ? (
    <div ref={setElement} className="captcha-widget my-2 flex min-h-[65px] justify-center" data-testid="captcha-widget" />
  ) : null;

  return {
    widget,
    token,
    required: Boolean(config),
    failed,
    reset,
    ready: config === null || config === undefined ? true : token !== null,
  };
}
