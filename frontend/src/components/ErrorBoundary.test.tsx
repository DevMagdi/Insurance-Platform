import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Boom({ message }: { message: string }): never { throw new Error(message); }

describe('ErrorBoundary', () => {
  it('renders its children when nothing throws', () => {
    render(<ErrorBoundary><p>العمليات والكتالوج</p></ErrorBoundary>);
    expect(screen.getByText('العمليات والكتالوج')).toBeTruthy();
  });

  it('degrades to the section fallback instead of the whole page', () => {
    // React logs the caught error; the point is that the tree survives.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(
      <ErrorBoundary fallback={<div role="alert">تعذر عرض الجدول</div>}>
        <Boom message="Cannot read properties of undefined (reading 'join')" />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('alert').textContent).toContain('تعذر عرض الجدول');
    expect(screen.queryByText('حدث خطأ غير متوقع')).toBeNull();
  });

  it('falls back to the full-page message when no fallback is given', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<ErrorBoundary><Boom message="boom" /></ErrorBoundary>);
    expect(screen.getByText('حدث خطأ غير متوقع')).toBeTruthy();
  });
});
