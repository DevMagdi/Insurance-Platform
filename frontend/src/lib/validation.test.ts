import { describe, expect, it } from 'vitest';
import {
  COMPANY_SLUG_PATTERN_SOURCE,
  LOGO_PATH_PATTERN_SOURCE,
  formatNationalId,
  invalidEmailList,
  isValidCompanySlug,
  isValidDateWindow,
  isValidEmail,
  isValidLogoPath,
  isValidMobile,
  isValidNationalId,
  isValidTicketNo,
  maskNationalId,
  nationalIdError,
  normalizeMobile,
  parseEmailList,
  toIsoDateTime,
} from './validation';

describe('Egyptian national ID (mirrors backend validator)', () => {
  it('accepts a structurally valid ID', () => {
    expect(isValidNationalId('29901010112345')).toBe(true);
    expect(nationalIdError('29901010112345')).toBeNull();
    expect(isValidNationalId('30112018800123'.slice(0, 14))).toBe(true); // born abroad (88)
  });

  it('rejects wrong length and non-digits', () => {
    expect(nationalIdError('2990101011234')).toBe('length');
    expect(nationalIdError('2990101011234x')).toBe('length');
  });

  it('rejects bad century, impossible dates, future births and governorates', () => {
    expect(nationalIdError('19901010112345')).toBe('century');
    expect(nationalIdError('29902300112345')).toBe('date'); // 30 Feb
    expect(nationalIdError('39901010112345')).toBe('future'); // 2099
    expect(nationalIdError('29901019912345')).toBe('governorate'); // 99
    expect(nationalIdError('29901013612345')).toBe('governorate'); // 36
  });

  it('masks all but the last four digits', () => {
    expect(maskNationalId('29901010112345')).toBe('•••• •••• ••23 45');
    expect(maskNationalId('29901010112345')).not.toContain('2990');
    expect(formatNationalId('29901010112345')).toBe('2990 1010 1123 45');
  });
});

describe('mobile normalization', () => {
  it('converts local Egyptian formats to +20 form', () => {
    expect(normalizeMobile('01123456789')).toBe('+201123456789');
    expect(normalizeMobile('201123456789')).toBe('+201123456789');
    expect(normalizeMobile('+20 112 345 6789')).toBe('+201123456789');
    expect(isValidMobile('01123456789')).toBe(true);
    expect(isValidMobile('02123456789')).toBe(false);
    expect(isValidMobile('12345')).toBe(false);
  });
});

describe('ticket numbers', () => {
  it('matches the backend CMP pattern', () => {
    expect(isValidTicketNo('CMP-2026-000123')).toBe(true);
    expect(isValidTicketNo('cmp-2026-000123')).toBe(false);
    expect(isValidTicketNo('CMP-26-1')).toBe(false);
  });
});

describe('contact addresses (mirrors the API validators)', () => {
  it('accepts a plain lowercase address', () => {
    expect(isValidEmail('claims@example.com')).toBe(true);
  });

  it('rejects an address without a domain or with a space', () => {
    expect(isValidEmail('not-an-address')).toBe(false);
    expect(isValidEmail('a b@example.com')).toBe(false);
    expect(isValidEmail('claims@example')).toBe(false);
  });

  it('normalizes and de-duplicates a comma separated list', () => {
    expect(parseEmailList(' A@Example.com , b@example.com ,a@example.com ')).toEqual([
      'a@example.com',
      'b@example.com',
    ]);
  });

  it('lists the addresses the API would reject', () => {
    expect(invalidEmailList('ok@example.com, broken')).toEqual(['broken']);
    expect(invalidEmailList('ok@example.com')).toEqual([]);
  });
});

describe('date windows (mirrors the export/analytics rules)', () => {
  const from = '2026-01-01T00:00:00.000Z';
  const to = '2026-01-30T00:00:00.000Z';

  it('accepts a complete window inside 366 days', () => {
    expect(isValidDateWindow(from, to)).toBe(true);
  });

  it('rejects an incomplete or inverted window', () => {
    expect(isValidDateWindow('', to)).toBe(false);
    expect(isValidDateWindow(from, '')).toBe(false);
    expect(isValidDateWindow(to, from)).toBe(false);
  });

  it('rejects a window longer than 366 days', () => {
    expect(isValidDateWindow('2024-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')).toBe(false);
  });

  it('turns a cleared date field into an empty value instead of throwing', () => {
    expect(toIsoDateTime('')).toBe('');
    expect(toIsoDateTime('not-a-date')).toBe('');
    expect(toIsoDateTime('2026-01-01T00:00')).toBe(new Date('2026-01-01T00:00').toISOString());
  });
});

describe('platform logo path (mirrors the API validator)', () => {
  it('accepts a same-origin asset path', () => {
    expect(isValidLogoPath('/insurance-logo.svg')).toBe(true);
    expect(isValidLogoPath('/api/public/brand/logo')).toBe(true);
  });

  it('rejects an absolute URL or a traversal', () => {
    expect(isValidLogoPath('https://cdn.example.com/logo.svg')).toBe(false);
    expect(isValidLogoPath('//cdn.example.com/logo.svg')).toBe(false);
    expect(isValidLogoPath('/../secret')).toBe(false);
    expect(isValidLogoPath('')).toBe(false);
  });
});

/**
 * Browsers compile an HTML `pattern` attribute as
 * `new RegExp('^(?:' + pattern + ')$', 'v')`. Under that `v` (unicodeSets) flag
 * a literal `/` or `-` inside a character class is a syntax error, the browser
 * logs "Invalid regular expression" and silently drops the constraint — which
 * is exactly what `^/(?!/)[A-Za-z0-9._/-]+$` did on the logo field.
 */
describe('HTML pattern sources (compiled with the browser `v` flag)', () => {
  const compile = (source: string, flags?: string) => new RegExp(`^(?:${source})$`, flags);

  it('compiles the shared sources in every regex mode a browser may use', () => {
    for (const source of [LOGO_PATH_PATTERN_SOURCE, COMPANY_SLUG_PATTERN_SOURCE]) {
      expect(() => compile(source, 'v')).not.toThrow();
      expect(() => compile(source, 'u')).not.toThrow();
      expect(() => compile(source)).not.toThrow();
    }
  });

  it('keeps the logo rule identical to the backend `safe_logo_path` validator', () => {
    const browser = compile(LOGO_PATH_PATTERN_SOURCE, 'v');
    const backend = /^\/(?!\/)[A-Za-z0-9._/-]+$/;
    const samples = [
      '/insurance-logo.svg', '/api/public/brand/logo', '/assets/brand/logo-2.webp', '/a/b_c-1.svg',
      '//cdn.example.com/logo.svg', 'https://cdn.example.com/logo.svg', 'logo.svg', '/', '',
      '/a b.svg', '/شعار.svg', '/a?b=1', '/a#b',
    ];
    for (const sample of samples) {
      expect(browser.test(sample), sample).toBe(backend.test(sample));
      expect(isValidLogoPath(sample), sample).toBe(backend.test(sample) && !sample.split('/').includes('..'));
    }
  });

  it('keeps the company slug rule identical to the backend `CompanySpec.slug` pattern', () => {
    const browser = compile(COMPANY_SLUG_PATTERN_SOURCE, 'v');
    const backend = /^[a-z][a-z0-9-]{1,59}$/;
    const samples = ['nile-assurance', 'n', 'nile1', '9nile', 'Nile', 'nile_1', 'nile assurance', '', `a${'b'.repeat(59)}`, `a${'b'.repeat(60)}`];
    for (const sample of samples) {
      expect(browser.test(sample), sample).toBe(backend.test(sample));
      expect(isValidCompanySlug(sample), sample).toBe(backend.test(sample));
    }
  });
});
