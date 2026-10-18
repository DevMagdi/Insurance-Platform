import { afterEach, describe, expect, it, vi } from 'vitest';
import { describeApiError } from './apiErrors';
import { api as consoleApi, ApiError as ConsoleApiError } from '../api';

afterEach(() => { vi.unstubAllGlobals(); });

describe('describeApiError', () => {
  it('names the field and the problem of a FastAPI validation list', () => {
    const detail = [{ type: 'missing', loc: ['body', 'profile', 'national_id'], msg: 'Field required' }];
    expect(describeApiError(422, detail)).toBe('الرقم القومي: حقل مطلوب');
  });

  it('translates the custom validator messages', () => {
    const detail = [{
      type: 'value_error',
      loc: ['body'],
      msg: 'Value error, Team lead must be included in the members',
    }];
    expect(describeApiError(422, detail)).toBe('قائد الفريق يجب أن يكون من أعضاء الفريق.');
  });

  it('mentions how many further issues there are', () => {
    const detail = [
      { type: 'string_too_short', loc: ['body', 'name'], msg: 'String should have at least 2 characters' },
      { type: 'missing', loc: ['body', 'email'], msg: 'Field required' },
    ];
    expect(describeApiError(422, detail)).toBe('الاسم: النص أقصر من المسموح (و1 ملاحظة أخرى)');
  });

  it('never blames the user for a missing technical header', () => {
    const detail = [{ type: 'missing', loc: ['header', 'Idempotency-Key'], msg: 'Field required' }];
    expect(describeApiError(422, detail)).toContain('حدّث الصفحة');
  });

  it('translates the details users meet in normal use', () => {
    expect(describeApiError(401, 'Invalid credentials')).toBe('البريد الإلكتروني أو كلمة المرور غير صحيحة.');
    expect(describeApiError(413, 'Narrow filters to 1000 complaints or fewer')).toContain('1000');
    expect(describeApiError(409, 'Identity cannot be registered')).toContain('الدعم');
    expect(describeApiError(409, 'Complaint cannot be reopened')).toContain('إعادة فتح');
    expect(describeApiError(503, 'Service temporarily unavailable; please retry shortly.')).toContain('مؤقتاً');
    expect(describeApiError(404, 'Ticket and national ID pair not found')).toContain('رقم الشكوى');
    expect(describeApiError(404, 'Complaint unavailable')).toContain('غير متاحة');
    expect(describeApiError(404, 'Department unavailable')).toContain('القسم');
    expect(describeApiError(404, 'Team not found')).toContain('الفريق');
    expect(describeApiError(401, 'Invalid or revoked API key')).toContain('مفتاح الواجهة');
    expect(describeApiError(503, 'Storage inventory is temporarily unavailable')).toContain('المخزن');
    expect(describeApiError(404, 'Privacy request unavailable')).toContain('الخصوصية');
    expect(describeApiError(422, 'Invalid customer profile')).toContain('الملف الشخصي');
    expect(describeApiError(413, 'Webhook payload is too large')).toContain('أكبر من المسموح');
  });

  it('keeps an unknown string detail as it is', () => {
    expect(describeApiError(400, 'Something specific')).toBe('Something specific');
  });

  it('falls back to an Arabic status message', () => {
    expect(describeApiError(422, null)).toContain('غير صالحة');
    expect(describeApiError(500, undefined)).toContain('الخادم');
    expect(describeApiError(429, {})).toContain('طلبات كثيرة');
  });
});

describe('console API client', () => {
  it('turns a 422 validation list into a readable message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(
      { detail: [{ type: 'string_pattern_mismatch', loc: ['body', 'slug'], msg: 'String should match pattern' }] },
      { status: 422 },
    )));
    const failure = await consoleApi('/api/staff/admin/companies', { method: 'POST', body: '{}' }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ConsoleApiError);
    const error = failure as ConsoleApiError;
    expect(error.status).toBe(422);
    expect(error.message).toBe('المعرّف المختصر: الصيغة غير صحيحة');
    expect(error.message).not.toContain('Request failed');
    expect(Array.isArray(error.raw)).toBe(true);
  });
});
