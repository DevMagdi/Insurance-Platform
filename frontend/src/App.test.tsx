import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';
import type { MeResponse, StaffAccess } from './types';

const staffAccess: StaffAccess = {
  id: 'staff-1', email: 'agent@example.invalid', full_name: 'Demo Agent', status: 'active',
  token_version: 1, two_factor_enabled: true, two_factor_required: true,
  roles: ['Agent'], permissions: ['complaints.view'], overrides: {}, all_companies: false,
  company_ids: ['company-1'], insurance_types: ['health'], ownership: 'own', team_ids: [],
};
const filterOptions = {
  companies: [{ id: 'company-1', name_en: 'One Insurer', name_ar: 'شركة النطاق', insurance_types: ['health'] }],
  agents: [], statuses: ['new'], priorities: ['normal'], channels: ['portal'], insurance_types: ['health'],
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, '', '/'); });

describe('broker dashboard shell', () => {
  it('loads effective access, routes a complaints-only user to their permitted page, and scopes the data request', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input); calls.push(path);
      if (path === '/api/me') return new Response(JSON.stringify({ kind: 'staff', access: staffAccess } satisfies MeResponse), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (path.startsWith('/api/staff/complaints/filter-options')) return new Response(JSON.stringify(filterOptions), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (path.startsWith('/api/staff/complaints?')) return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (path === '/api/staff/views') return new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (path === '/api/me/notifications') return new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } });
      return new Response(JSON.stringify({ detail: 'Not found' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    }));

    render(<App />);
    expect(await screen.findByRole('heading', { name: 'صندوق الشكاوى' })).toBeTruthy();
    expect(await screen.findByText(/لا توجد شكاوى مطابقة/)).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'مساحة العمل' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'لوحة المؤشرات' })).toBeNull();
    expect(screen.queryByRole('button', { name: /CSV · Excel-ready/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /PDF \/ طباعة/ })).toBeNull();
    await waitFor(() => expect(calls.some((path) => path.includes('company_id=company-1'))).toBe(false));
    expect(calls.some((path) => path.startsWith('/api/staff/complaints?') && path.includes('company_id='))).toBe(false);
  });

  it('renders a refreshable sign-in route when no staff session is present', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Session expired' }), { status: 401, headers: { 'Content-Type': 'application/json' } })));
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'مرحباً بعودتك' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /تسجيل الدخول/ })).toBeTruthy();
  });
});
