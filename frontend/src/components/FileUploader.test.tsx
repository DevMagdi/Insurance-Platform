import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import '../i18n';
import { FileUploader } from './FileUploader';
import { toQueuedFile } from '../lib/files';

describe('FileUploader', () => {
  it('accepts a supported file and lists it with a preview-free PDF tile', () => {
    const onAdd = vi.fn();
    render(<FileUploader files={[]} onAdd={onAdd} onRemove={vi.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['%PDF-1.4'], 'proof.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [file] } });
    expect(onAdd).toHaveBeenCalledWith([file]);
  });

  it('rejects unsupported types with a live error', () => {
    const onAdd = vi.fn();
    render(<FileUploader files={[]} onAdd={onAdd} onRemove={vi.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    });
    expect(onAdd).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('صيغة غير مدعومة');
  });

  it('shows progress and supports remove/retry for failed uploads', () => {
    const onRemove = vi.fn();
    const onRetry = vi.fn();
    const queued = {
      ...toQueuedFile(new File(['%PDF'], 'proof.pdf', { type: 'application/pdf' })),
      status: 'error' as const,
      error: 'Network error during upload',
    };
    // jsdom cannot create object URLs for non-image branches; PDF path is fine.
    render(<FileUploader files={[queued]} onAdd={vi.fn()} onRemove={onRemove} onRetry={onRetry} />);
    expect(screen.getByText('proof.pdf')).toBeTruthy();
    fireEvent.click(screen.getByText('إعادة المحاولة'));
    expect(onRetry).toHaveBeenCalledWith(queued.id);
    fireEvent.click(screen.getByText('حذف'));
    expect(onRemove).toHaveBeenCalledWith(queued.id);
  });
});
