import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { StaffAccess } from '../../types';
import { StoragePage } from './StoragePage';

vi.mock('../../api', () => ({
  api: vi.fn(),
  queryString: (values: Record<string, string | number | undefined | null>) => {
    const params = new URLSearchParams();
    Object.entries(values).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    });
    return params.size ? `?${params.toString()}` : '';
  },
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const access: StaffAccess = {
  id: 'admin-1', email: 'admin@example.invalid', full_name: 'Admin', status: 'active', token_version: 1,
  two_factor_enabled: true, two_factor_required: true, roles: ['administrator'], permissions: ['storage.manage'],
  overrides: {}, all_companies: true, company_ids: [], insurance_types: [], ownership: 'all', team_ids: [],
};

const totals = {
  objects: 3, bytes: 4608, pending_scans: 1, attachment_metadata_without_content: 0,
  by_kind: {
    attachment: { objects: 2, bytes: 4096, attention: 1, missing_content: 0 },
    export: { objects: 1, bytes: 512, attention: 0, missing_content: 0 },
    recording: { objects: 0, bytes: 0, attention: 0, missing_content: 0 },
  },
};

const attachmentId = '11111111-1111-4111-8111-111111111111';

const rootListing = {
  backend: 'postgresql', store: 'insurance-platform', status: 'online' as const, prefix: '',
  folders: ['attachments/', 'exports/', 'recordings/'],
  objects: [{
    key: `attachments/${attachmentId}`, kind: 'attachment', size_bytes: 2048,
    last_modified: '2026-10-15T12:00:00Z', checksum_sha256: 'a'.repeat(64),
    details: { filename: 'sample-report.pdf', mime_type: 'application/pdf', scan_status: 'clean', encrypted: true },
  }],
  totals, is_truncated: true, next_page_token: 'cGFnZS10b2tlbi8xKw',
};

const attachmentListing = {
  ...rootListing,
  prefix: 'attachments/',
  folders: [],
  objects: [{
    key: `attachments/${attachmentId}`, kind: 'attachment', size_bytes: 4096,
    last_modified: '2026-10-15T13:00:00Z', checksum_sha256: 'b'.repeat(64),
    details: { filename: 'file.pdf', mime_type: 'application/pdf', scan_status: 'clean', encrypted: true },
  }],
  is_truncated: false, next_page_token: null,
};

describe('StoragePage', () => {
  it('browses database rows, enters a logical folder and shows row metadata', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes('prefix=attachments')) return attachmentListing as never;
      return rootListing as never;
    });

    render(<StoragePage access={access} />);
    expect(await screen.findByRole('heading', { name: 'insurance-platform' })).toBeTruthy();
    expect(screen.getByText(/PostgreSQL/)).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'فتح المجلد attachments' })).toBeTruthy();
    expect(screen.getByText('sample-report.pdf')).toBeTruthy();
    // Totals come from the database, not from an object listing.
    expect(screen.getByText('صفوف مخزَّنة في قاعدة البيانات').parentElement?.textContent).toContain('٣');
    expect(screen.getByText('حجم المحتوى المشفَّر').parentElement?.textContent).toMatch(/كيلوبايت/);
    expect(screen.getByText('مرفقات لم يكتمل فحصها الأمني بعد')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'فتح المجلد attachments' }));
    expect(await screen.findByText('file.pdf')).toBeTruthy();
    expect(vi.mocked(api).mock.calls.some(([path]) => typeof path === 'string' && path.includes('prefix=attachments%2F'))).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: `تفاصيل attachments/${attachmentId}` }));
    expect(screen.getByRole('dialog', { name: 'تفاصيل الصف' })).toBeTruthy();
    expect(screen.getByText('b'.repeat(64))).toBeTruthy();
    expect(screen.getByText('حالة الفحص الأمني')).toBeTruthy();
    expect(screen.getByText(/لا يمكن حذف الملفات أو تعديلها/)).toBeTruthy();
    expect(screen.getByText(/مشفَّراً داخل قاعدة البيانات/)).toBeTruthy();
  });

  it('loads a bounded continuation page without replacing earlier rows', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes('continuation_token=')) {
        return {
          ...rootListing,
          folders: [],
          objects: [{
            key: 'exports/22222222-2222-4222-8222-222222222222', kind: 'export', size_bytes: 12,
            last_modified: null, checksum_sha256: null, details: { format: 'xlsx', encrypted: true },
          }],
          is_truncated: false, next_page_token: null,
        } as never;
      }
      return rootListing as never;
    });

    render(<StoragePage access={access} />);
    expect(await screen.findByText('sample-report.pdf')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'تحميل المزيد' }));
    expect(await screen.findByText('22222222-2222-4222-8222-222222222222')).toBeTruthy();
    expect(screen.getByText('sample-report.pdf')).toBeTruthy();
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) =>
      typeof path === 'string' && path.includes('continuation_token=cGFnZS10b2tlbi8xKw'),
    )).toBe(true));
  });

  it('requires a storage permission and global staff scope before making API requests', async () => {
    render(<StoragePage access={{ ...access, all_companies: false, company_ids: ['company-1'] }} />);
    expect(await screen.findByText('مركز التخزين غير متاح')).toBeTruthy();
    expect(vi.mocked(api)).not.toHaveBeenCalled();

    cleanup();
    render(<StoragePage access={{ ...access, permissions: [] }} />);
    expect(await screen.findByText('مركز التخزين غير متاح')).toBeTruthy();
    expect(vi.mocked(api)).not.toHaveBeenCalled();
  });
});
