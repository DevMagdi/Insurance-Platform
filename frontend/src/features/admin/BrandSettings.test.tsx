import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { BrandSettings } from './BrandSettings';

vi.mock('../../api', () => ({ api: vi.fn() }));
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
    const input = await screen.findByLabelText(/رابط\/مسار الشعار/);
    fireEvent.change(input, { target: { value: 'https://cdn.example.com/logo.svg' } });
    fireEvent.click(screen.getByRole('button', { name: /حفظ الهوية/ }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('مسار الشعار'));
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/api/platform/brand')).toBe(false);
  });
});
