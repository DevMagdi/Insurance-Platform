import { describe, expect, it } from 'vitest';
import { isPublicHttpsUrl, validateIntegrationCredentials } from './integrationRules';

describe('public HTTPS callbacks', () => {
  it('accepts a public endpoint', () => {
    expect(isPublicHttpsUrl('https://hooks.example.com/insurance')).toBe(true);
  });

  it('rejects plain HTTP, credentials and private hosts', () => {
    expect(isPublicHttpsUrl('http://hooks.example.com/insurance')).toBe(false);
    expect(isPublicHttpsUrl('https://user:pass@hooks.example.com/x')).toBe(false);
    expect(isPublicHttpsUrl('https://localhost/hook')).toBe(false);
    expect(isPublicHttpsUrl('https://service.internal/hook')).toBe(false);
    expect(isPublicHttpsUrl('not-a-url')).toBe(false);
  });
});

describe('integration credentials (mirrors IntegrationConfigSpec)', () => {
  it('accepts a valid SMTP configuration', () => {
    expect(
      validateIntegrationCredentials('smtp', {
        host: 'smtp.example.com',
        port: '587',
        start_tls: 'true',
        use_tls: 'false',
        username: 'claims@example.com',
        password: 'secret',
        from_email: 'claims@example.com',
      }),
    ).toEqual([]);
  });

  it('rejects a port outside 1..65535', () => {
    const issues = validateIntegrationCredentials('smtp', { port: '70000' });
    expect(issues.map((issue) => issue.field)).toContain('port');
  });

  it('rejects STARTTLS together with implicit TLS', () => {
    const issues = validateIntegrationCredentials('smtp', { start_tls: 'true', use_tls: 'true' });
    expect(issues.map((issue) => issue.field)).toContain('use_tls');
  });

  it('rejects an invalid sender address', () => {
    const issues = validateIntegrationCredentials('smtp', { from_email: 'claims-at-example' });
    expect(issues.map((issue) => issue.field)).toContain('from_email');
  });

  it('rejects a plain HTTP endpoint', () => {
    const issues = validateIntegrationCredentials('openai', { base_url: 'http://api.example.com' });
    expect(issues.map((issue) => issue.field)).toContain('base_url');
  });

  it('rejects a private status callback', () => {
    const issues = validateIntegrationCredentials('twilio_sms', {
      status_callback_url: 'http://localhost/hook',
    });
    expect(issues.map((issue) => issue.field)).toContain('status_callback_url');
  });

  it('rejects an invalid country allowlist', () => {
    const issues = validateIntegrationCredentials('twilio_sms', { allowed_country_codes: '20,966' });
    expect(issues.map((issue) => issue.field)).toContain('allowed_country_codes');
  });

  it('rejects an invalid sandbox recipient', () => {
    const issues = validateIntegrationCredentials('twilio_sms', { sandbox_recipient: 'nope' });
    expect(issues.map((issue) => issue.field)).toContain('sandbox_recipient');
  });

  it('rejects an IMAP password for a Gmail mailbox', () => {
    const issues = validateIntegrationCredentials('imap', { username: 'agent@gmail.com' });
    expect(issues.map((issue) => issue.field)).toContain('username');
  });
});
