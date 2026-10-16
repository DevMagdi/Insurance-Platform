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

const rootListing = {
  bucket: 'complaint-attachments', provider: 'S3-compatible', status: 'online' as const, prefix: '',
  folders: ['attachments/'],
  objects: [{ key: 'sample-report.pdf', size_bytes: 2048, last_modified: '2026-10-15T12:00:00Z', etag: '"abc123"' }],
  is_truncated: true, next_page_token: 'page-token/1+',
};

const attachmentListing = {
  ...rootListing,
  prefix: 'attachments/',
  folders: [],
  objects: [{ key: 'attachments/9f3e0d/file.pdf', size_bytes: 4096, last_modified: '2026-10-15T13:00:00Z', etag: '"ef01"' }],
  is_truncated: false, next_page_token: null,
};

describe('StoragePage', () => {
  it('browses the configured bucket, enters folders and displays object metadata', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes('prefix=attachments')) return attachmentListing as never;
      return rootListing as never;
    });

    render(<StoragePage access={access} />);
    expect(await screen.findByRole('heading', { name: 'complaint-attachments' })).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'فتح المجلد attachments' })).toBeTruthy();
    expect(screen.getByText('sample-report.pdf')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'فتح المجلد attachments' }));
    expect(await screen.findByText('file.pdf')).toBeTruthy();
    expect(vi.mocked(api).mock.calls.some(([path]) => typeof path === 'string' && path.includes('prefix=attachments%2F'))).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'تفاصيل attachments/9f3e0d/file.pdf' }));
    expect(screen.getByRole('dialog', { name: 'تفاصيل الكائن' })).toBeTruthy();
    expect(screen.getByText('"ef01"')).toBeTruthy();
    expect(screen.getByText(/لا يمكن حذف الملفات أو تعديلها/)).toBeTruthy();
    expect(screen.getByText(/يبقى محتوى الملفات ضمن مسارات التنزيل/)).toBeTruthy();
  });

  it('loads a bounded continuation page without replacing earlier object rows', async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes('continuation_token=')) {
        return { ...rootListing, objects: [{ key: 'next-page.txt', size_bytes: 12, last_modified: null, etag: null }], is_truncated: false, next_page_token: null } as never;
      }
      return rootListing as never;
    });

    render(<StoragePage access={access} />);
    expect(await screen.findByText('sample-report.pdf')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'تحميل المزيد' }));
    expect(await screen.findByText('next-page.txt')).toBeTruthy();
    expect(screen.getByText('sample-report.pdf')).toBeTruthy();
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) =>
      typeof path === 'string' && path.includes('continuation_token=page-token%2F1%2B'),
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
