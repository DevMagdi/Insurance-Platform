import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { BrandSettings } from './BrandSettings';

vi.mock('../../api', () => ({
  api: vi.fn(),
  jsonBody: (value: unknown) => JSON.stringify(value),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['administrator'], permissions: ['settings.manage'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

describe('BrandSettings', () => {
  it('uploads a logo file and applies the returned public URL', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ platform_name: 'Insurance', logo_url: '/insurance-logo.svg' }) }));
    vi.mocked(api).mockResolvedValue({ logo_url: '/api/public/brand/logo' } as never);
    render(<BrandSettings access={access} />);
    const file = new File(['image'], 'brand.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText(/أو ارفع صورة شعار/), { target: { files: [file] } });
    expect((await screen.findByRole('status')).textContent).toContain('تم رفع الشعار وتحديث هوية المنصة.');
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/api/platform/brand/logo', expect.objectContaining({ method: 'POST', body: file })));
    await waitFor(() => expect(screen.getByRole('img', { name: 'معاينة الشعار' }).getAttribute('src')).toBe('/api/public/brand/logo'));
  });
  it('refuses a logo path that points outside the platform', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ platform_name: 'Insurance', logo_url: '/insurance-logo.svg' }) }));
    vi.mocked(api).mockResolvedValue({ logo_url: '/api/public/brand/logo' } as never);
    render(<BrandSettings access={access} />);
    const input = await screen.findByLabelText(/رابط\/مسار الشعار/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'https://cdn.example.com/logo.svg' } });
    // The native constraint is enforceable again: while the pattern was an
    // invalid `v`-flag regex the engine ignored it and any value looked valid.
    expect(input.checkValidity()).toBe(false);
    fireEvent.change(input, { target: { value: '/insurance-logo.svg' } });
    expect(input.checkValidity()).toBe(true);
    fireEvent.change(input, { target: { value: 'https://cdn.example.com/logo.svg' } });
    // A submit that bypasses the native check still meets the JS guard, which
    // explains the rule in Arabic instead of sending a request the API rejects.
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('مسار الشعار'));
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/api/platform/brand')).toBe(false);
  });

  it('exposes a logo pattern a browser can compile (unicodeSets `v` flag)', async () => {
    // Regression: the field shipped `pattern="^/(?!/)[A-Za-z0-9._/-]+$"`, which
    // the browser rejects ("Invalid regular expression: Invalid character in
    // character class") because `pattern` is compiled with the `v` flag, where a
    // literal `/` or `-` inside a class must be escaped. The constraint was then
    // dropped and the console logged the error on every render.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ platform_name: 'Insurance', logo_url: '/insurance-logo.svg' }) }));
    render(<BrandSettings access={access} />);
    const input = await screen.findByLabelText(/رابط\/مسار الشعار/);
    const pattern = (input as HTMLInputElement).pattern;
    expect(pattern).toBeTruthy();
    const compiled = new RegExp(`^(?:${pattern})$`, 'v');
    expect(compiled.test('/insurance-logo.svg')).toBe(true);
    expect(compiled.test('/api/public/brand/logo')).toBe(true);
    expect(compiled.test('https://cdn.example.com/logo.svg')).toBe(false);
    expect(compiled.test('//cdn.example.com/logo.svg')).toBe(false);
  });

  it('PUTs only the PlatformBrandRequest fields when public config carries captcha/privacy extras', async () => {
    // Reproduces the 422 bug: /api/public/config also returns `captcha` and
    // `privacy_policy_version`; echoing them back into PUT /api/platform/brand
    // makes the strict backend reject the save with "Extra inputs are not permitted".
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        captcha: { site_key: 'test-site-key', provider: 'turnstile' },
        privacy_policy_version: '2026-10',
        platform_name: 'Insurance',
        logo_url: '/insurance-logo.svg',
      }),
    }));
    vi.mocked(api).mockImplementation((async (_path: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      // Enforce the same strict contract as the backend StrictModel (extra="forbid").
      const extra = Object.keys(body).filter((key) => !['platform_name', 'logo_url'].includes(key));
      if (extra.length > 0) {
        throw new Error(`422 Unprocessable Entity: Extra inputs are not permitted: ${extra.join(', ')}`);
      }
      return { platform_name: body.platform_name, logo_url: body.logo_url };
    }) as never);
    render(<BrandSettings access={access} />);
    const nameInput = await screen.findByLabelText('اسم المنصة');
    await waitFor(() => expect(nameInput).toHaveProperty('value', 'Insurance'));
    fireEvent.change(nameInput, { target: { value: 'منصة التأمين المصرية' } });
    fireEvent.click(screen.getByRole('button', { name: /حفظ الهوية/ }));
    expect((await screen.findByRole('status')).textContent).toContain('تم تحديث هوية المنصة بنجاح.');
    expect(screen.queryByRole('alert')).toBeNull();
    const putCall = vi.mocked(api).mock.calls.find(([path]) => path === '/api/platform/brand');
    expect(putCall).toBeDefined();
    expect(JSON.parse(String(putCall![1]?.body))).toEqual({
      platform_name: 'منصة التأمين المصرية',
      logo_url: '/insurance-logo.svg',
    });
    expect(screen.getByRole('img', { name: 'معاينة الشعار' }).getAttribute('src')).toBe('/insurance-logo.svg');
  });
});
