import { describe, expect, it } from 'vitest';
import {
  ACCEPTED_MIME,
  MAX_FILES,
  hasUnsafeFileName,
  validateNewFiles,
} from './files';

function file(name: string, size = 1024, type = 'image/png'): File {
  return { name, size, type } as File;
}

describe('attachment rules (mirrors the upload plan)', () => {
  it('accepts a normal file', () => {
    expect(validateNewFiles([], [file('receipt.png')])).toBeNull();
  });

  it('rejects too many files', () => {
    const existing = Array.from({ length: MAX_FILES }, (_, index) => file(`a${index}.png`));
    expect(validateNewFiles(existing, [file('b.png')])).toBe('tooMany');
  });

  it('rejects an unsupported type', () => {
    expect(validateNewFiles([], [file('notes.txt', 10, 'text/plain')])).toBe('badType');
  });

  it('rejects an oversized file', () => {
    expect(validateNewFiles([], [file('big.png', 11_000_000)])).toBe('tooLarge');
  });

  it('rejects a file name that carries a path', () => {
    expect(hasUnsafeFileName('a/b.png')).toBe(true);
    expect(hasUnsafeFileName('a\\b.png')).toBe(true);
    expect(hasUnsafeFileName('ab.png')).toBe(true);
    expect(hasUnsafeFileName('receipt.png')).toBe(false);
    expect(validateNewFiles([], [file('a\\b.png')])).toBe('badName');
  });

  it('only accepts the documented media types', () => {
    expect(ACCEPTED_MIME).toContain('application/pdf');
    expect(ACCEPTED_MIME).toContain('image/png');
    expect(ACCEPTED_MIME).toContain('image/jpeg');
  });
});
