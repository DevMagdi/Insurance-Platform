import { describe, expect, it } from 'vitest';
import {
  formatNationalId,
  isValidMobile,
  isValidNationalId,
  isValidTicketNo,
  maskNationalId,
  nationalIdError,
  normalizeMobile,
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
