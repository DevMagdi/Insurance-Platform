/**
 * Client-side mirror of the API's integration credential rules.
 *
 * `IntegrationConfigSpec` (backend/app/domain/schemas.py) rejects an invalid
 * credential set with 422. The operations console checks the very same rules
 * before sending the request so the operator gets a readable Arabic message
 * instead of a raw validation payload.
 */

export interface CredentialIssue {
  /** The credential key the problem belongs to. */
  field: string;
  message: string;
}

const TRUTHY = new Set(['true', '1', 'yes']);

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function isTruthy(value: string | undefined): boolean {
  return TRUTHY.has((value ?? 'false').trim().toLowerCase());
}

/** A public HTTPS callback: no credentials, no private host. */
export function isPublicHttpsUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:' || !parsed.hostname) return false;
  if (parsed.username || parsed.password) return false;
  const host = parsed.hostname.toLowerCase();
  if (host === 'localhost' || host === 'metadata.google.internal') return false;
  if (host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal')) {
    return false;
  }
  return true;
}

export function validateIntegrationCredentials(
  provider: string,
  credentials: Record<string, string>,
): CredentialIssue[] {
  const issues: CredentialIssue[] = [];
  const add = (field: string, message: string) => issues.push({ field, message });

  const port = credentials.port?.trim();
  if (port) {
    if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
      add('port', 'المنفذ يجب أن يكون رقماً بين 1 و 65535.');
    }
  }

  if (isTruthy(credentials.use_tls) && isTruthy(credentials.start_tls)) {
    add('use_tls', 'لا يمكن تفعيل STARTTLS وTLS المباشر في الوقت نفسه.');
  }

  const fromEmail = credentials.from_email?.trim();
  if (fromEmail && !EMAIL.test(fromEmail)) {
    add('from_email', 'عنوان المرسل غير صالح.');
  }

  const baseUrl = credentials.base_url?.trim();
  if (baseUrl && !baseUrl.startsWith('https://')) {
    add('base_url', 'الرابط الأساسي يجب أن يبدأ بـ https://');
  }

  const callback = credentials.status_callback_url?.trim();
  if (callback && !isPublicHttpsUrl(callback)) {
    add('status_callback_url', 'رابط استقبال الأحداث يجب أن يكون HTTPS عاماً بلا بيانات دخول.');
  }

  const mailbox = credentials.mailbox?.trim();
  if (mailbox && !/^[A-Za-z0-9_./ -]{1,128}$/.test(mailbox)) {
    add('mailbox', 'اسم صندوق البريد غير صالح.');
  }

  const countries = credentials.allowed_country_codes?.trim();
  if (countries && !/^\+\d{1,4}(,\+\d{1,4}){0,24}$/.test(countries)) {
    add('allowed_country_codes', 'صيغة الدول المسموح بها غير صحيحة (مثال: +20,+966).');
  }

  const sandbox = credentials.sandbox_recipient?.trim();
  if (sandbox && !EMAIL.test(sandbox) && !/^\+?[1-9]\d{6,14}$/.test(sandbox)) {
    add('sandbox_recipient', 'رقم أو بريد الاختبار (sandbox) غير صالح.');
  }

  if (
    provider === 'imap' &&
    /.*@(gmail|googlemail|outlook|hotmail|live|office365)\.(com|[a-z]{2,})/i.test(
      credentials.username ?? '',
    )
  ) {
    add('username', 'Gmail وOutlook يحتاجان ربط OAuth، وليس كلمة مرور IMAP.');
  }

  return issues;
}
