/**
 * Real provider connection management for the staff operations console.
 *
 * Credentials are partial updates: secret fields are never prefilled from the
 * server (the API does not return them) and blank fields keep stored values.
 * Every provider exposes a non-mutating connection test with an explicit state:
 * غير مضبوط / مضبوط ولم يُختبر / متصل / فشل. Only providers with a real
 * implementation are configurable; unsupported features are listed as disabled.
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal, SectionTitle } from '../../components/Modal';
import { formatDateTime } from '../../utils/date';
import type { CompanyOption } from '../../types';

export type ConnectionStatus = 'unconfigured' | 'configured' | 'connected' | 'failed';

export interface IntegrationConnectionRecord {
  id: string;
  company_id: string | null;
  provider: string;
  is_enabled: boolean;
  has_credentials: boolean;
  updated_at: string;
  connection_status: ConnectionStatus;
  last_test_at: string | null;
  last_error_code: string | null;
}

interface FieldSpec {
  key: string;
  label: string;
  secret?: boolean;
  boolean?: boolean;
  placeholder?: string;
  help?: string;
}

interface ProviderSpec {
  id: string;
  label: string;
  icon: 'mail' | 'message' | 'phone' | 'spark' | 'lock' | 'shield';
  family: 'email' | 'messaging' | 'ai' | 'voice' | 'security';
  summary: string;
  scope: 'global' | 'company' | 'both';
  oauth?: 'gmail_oauth' | 'outlook_oauth';
  fields: FieldSpec[];
}

const PROVIDERS: ProviderSpec[] = [
  {
    id: 'smtp',
    label: 'بريد SMTP عام',
    icon: 'mail',
    family: 'email',
    summary: 'إرسال عبر أي خادم SMTP (بما فيه Mailpit محلياً). الاستقبال عبر IMAP منفصل.',
    scope: 'both',
    fields: [
      { key: 'host', label: 'خادم SMTP', placeholder: 'smtp.example.com' },
      { key: 'port', label: 'المنفذ', placeholder: '587' },
      { key: 'start_tls', label: 'STARTTLS', boolean: true },
      { key: 'username', label: 'اسم المستخدم' },
      { key: 'password', label: 'كلمة المرور', secret: true },
      { key: 'from_email', label: 'المرسل من', placeholder: 'claims@example.com' },
    ],
  },
  {
    id: 'gmail_oauth',
    label: 'Gmail — Google OAuth 2.0',
    icon: 'mail',
    family: 'email',
    summary: 'الإرسال والاستقبال عبر Gmail API بحساب خدمة Gmail. يتطلب OAuth؛ لا يدعم كلمات مرور التطبيقات.',
    scope: 'company',
    oauth: 'gmail_oauth',
    fields: [
      { key: 'client_id', label: 'OAuth client_id' },
      { key: 'client_secret', label: 'OAuth client_secret', secret: true },
      {
        key: 'refresh_token',
        label: 'refresh_token',
        secret: true,
        help: 'يمكن استبداله بزر «ربط حساب Google» بعد حفظ client_id/secret.',
      },
      { key: 'from_email', label: 'المرسل من', placeholder: 'claims@example.com' },
      { key: 'mailbox', label: 'صندوق الاستقبال (اختياري)', placeholder: 'INBOX' },
    ],
  },
  {
    id: 'outlook_oauth',
    label: 'Outlook / Microsoft 365 — Graph OAuth 2.0',
    icon: 'mail',
    family: 'email',
    summary: 'الإرسال عبر Microsoft Graph (sendMail) والاستقبال عبر /me/messages. OAuth إلزامي.',
    scope: 'company',
    oauth: 'outlook_oauth',
    fields: [
      { key: 'client_id', label: 'OAuth client_id' },
      { key: 'client_secret', label: 'OAuth client_secret', secret: true },
      {
        key: 'refresh_token',
        label: 'refresh_token',
        secret: true,
        help: 'يمكن استبداله بزر «ربط حساب Microsoft» بعد حفظ client_id/secret.',
      },
      { key: 'from_email', label: 'المرسل من', placeholder: 'claims@example.com' },
      { key: 'tenant', label: 'Tenant', placeholder: 'common' },
      { key: 'mailbox', label: 'صندوق الاستقبال (اختياري)', placeholder: 'INBOX' },
    ],
  },
  {
    id: 'sendgrid',
    label: 'SendGrid',
    icon: 'mail',
    family: 'email',
    summary: 'إرسال عبر REST مع أحداث تسليم عبر webhook البريد.',
    scope: 'both',
    fields: [
      { key: 'api_key', label: 'API key', secret: true },
      { key: 'from_email', label: 'المرسل من', placeholder: 'claims@example.com' },
    ],
  },
  {
    id: 'ses',
    label: 'Amazon SES',
    icon: 'mail',
    family: 'email',
    summary: 'الإرسال عبر واجهة SES SMTP الإقليمية.',
    scope: 'both',
    fields: [
      { key: 'region', label: 'المنطقة', placeholder: 'eu-west-1' },
      { key: 'from_email', label: 'المرسل من', placeholder: 'claims@example.com' },
      { key: 'smtp_username', label: 'SES SMTP username' },
      { key: 'smtp_password', label: 'SES SMTP password', secret: true },
    ],
  },
  {
    id: 'imap',
    label: 'استقبال بريد IMAP',
    icon: 'mail',
    family: 'email',
    summary: 'سحب البريد الوارد لصناديق عامة. لا يُستخدم مع Gmail/Outlook — استخدم مزودَي OAuth.',
    scope: 'company',
    fields: [
      { key: 'host', label: 'خادم IMAP', placeholder: 'imap.example.com' },
      { key: 'port', label: 'المنفذ', placeholder: '993' },
      { key: 'username', label: 'اسم المستخدم' },
      { key: 'password', label: 'كلمة المرور', secret: true },
      { key: 'mailbox', label: 'الصندوق', placeholder: 'INBOX' },
      { key: 'use_ssl', label: 'SSL/TLS', boolean: true },
    ],
  },
  {
    id: 'whatsapp_cloud',
    label: 'Meta WhatsApp Cloud API',
    icon: 'message',
    family: 'messaging',
    summary: 'إرسال قوالب ورسائل داخل نافذة الخدمة، مع webhook موقّع وتحقق اشتراك.',
    scope: 'company',
    fields: [
      { key: 'phone_number_id', label: 'phone_number_id' },
      { key: 'access_token', label: 'Access token', secret: true },
      { key: 'app_secret', label: 'App secret (توقيع webhook)', secret: true },
      { key: 'verify_token', label: 'Verify token للاشتراك', secret: true },
      { key: 'api_version', label: 'إصدار الواجهة', placeholder: 'v21.0' },
    ],
  },
  {
    id: 'twilio_sms',
    label: 'SMS — Twilio',
    icon: 'message',
    family: 'messaging',
    summary: 'إرسال SMS مع تحديثات تسليم موقّعة. يلزم إعلان الدول المُتحقق من توفر الخدمة فيها.',
    scope: 'both',
    fields: [
      { key: 'account_sid', label: 'Account SID' },
      { key: 'auth_token', label: 'Auth token', secret: true },
      { key: 'from_number', label: 'الرقم المرسل', placeholder: '+201000000000' },
      { key: 'status_callback_url', label: 'رابط استقبال التسليم (HTTPS)' },
      {
        key: 'allowed_country_codes',
        label: 'الدول المسموح بها',
        placeholder: '+20,+966',
        help: 'أدخل فقط الدول التي تأكدت من توفر خدمة Twilio SMS فيها (مثال: +20 لمصر).',
      },
      {
        key: 'sandbox_recipient',
        label: 'رقم/بريد الاختبار (sandbox)',
        help: 'لا يُرسل أي اختبار إلا لهذا العنوان وبموافقة صريحة.',
      },
    ],
  },
  {
    id: 'openai',
    label: 'الذكاء الاصطناعي — OpenAI',
    icon: 'spark',
    family: 'ai',
    summary: 'تحليل الشكاوى باقتراحات تُراجع بشرياً دائماً. بيانات الشخصيات تُخفّض قبل الإرسال.',
    scope: 'both',
    fields: [
      { key: 'api_key', label: 'API key', secret: true },
      { key: 'model', label: 'النموذج', placeholder: 'gpt-4o-mini' },
      { key: 'base_url', label: 'الرابط الأساسي (HTTPS)', placeholder: 'https://api.openai.com/v1' },
      { key: 'timeout_seconds', label: 'مهلة الطلب (ثوانٍ)', placeholder: '30' },
      { key: 'max_retries', label: 'إعادة المحاولات', placeholder: '2' },
      { key: 'max_tokens', label: 'حد التوليد (tokens)', placeholder: '1024' },
      { key: 'daily_request_limit', label: 'الحد اليومي للطلبات', placeholder: '500' },
    ],
  },
  {
    id: 'twilio',
    label: 'المكالمات الصوتية — Twilio',
    icon: 'phone',
    family: 'voice',
    summary: 'مكالمات متابعة بموافقة مسجلة مسبقاً؛ STT/TTS فعليان عبر TwiML والتقاط الكلام.',
    scope: 'both',
    fields: [
      { key: 'account_sid', label: 'Account SID' },
      { key: 'auth_token', label: 'Auth token', secret: true },
      { key: 'from_number', label: 'الرقم المرسل', placeholder: '+201000000000' },
      { key: 'status_callback_url', label: 'رابط حالة المكالمة (HTTPS)' },
    ],
  },
  {
    id: 'vonage',
    label: 'المكالمات الصوتية — Vonage',
    icon: 'phone',
    family: 'voice',
    summary: 'مكالمات متابعة عبر Vonage Voice API بحساب تطبيق وتوقيع JWT.',
    scope: 'both',
    fields: [
      { key: 'application_id', label: 'Application ID' },
      { key: 'private_key', label: 'المفتاح الخاص (RSA PEM)', secret: true },
      { key: 'from_number', label: 'الرقم المرسل', placeholder: '+201000000000' },
      { key: 'status_callback_url', label: 'رابط الأحداث (HTTPS)' },
    ],
  },
  {
    id: 'openai_speech',
    label: 'STT/TTS — OpenAI Speech',
    icon: 'spark',
    family: 'voice',
    summary: 'تحويل الكلام إلى نص (Whisper) ونص إلى كلام (TTS) لتدفقات المكالمات.',
    scope: 'both',
    fields: [
      { key: 'api_key', label: 'API key', secret: true },
      { key: 'stt_model', label: 'نموذج STT', placeholder: 'whisper-1' },
      { key: 'tts_model', label: 'نموذج TTS', placeholder: 'gpt-4o-mini-tts' },
      { key: 'voice', label: 'الصوت', placeholder: 'alloy' },
      { key: 'base_url', label: 'الرابط الأساسي (HTTPS)', placeholder: 'https://api.openai.com/v1' },
    ],
  },
  {
    id: 'clamav',
    label: 'فحص الملفات — ClamAV',
    icon: 'shield',
    family: 'security',
    summary: 'ماسح ClamAV حقيقي عبر INSTREAM. الملف يبقى غير موثوق حتى يرجع الماسح «نظيف».',
    scope: 'global',
    fields: [
      { key: 'host', label: 'خادم clamd', placeholder: 'clamav:3310' },
      { key: 'port', label: 'المنفذ', placeholder: '3310' },
      { key: 'timeout_seconds', label: 'مهلة الفحص (ثوانٍ)', placeholder: '30' },
    ],
  },
];

const NOT_IMPLEMENTED: Array<{ title: string; detail: string }> = [
  {
    title: 'مخزن أسرار خارجي (Vault / Secrets Manager)',
    detail:
      'غير مفعّل: لا يوجد ربط حقيقي بمخزن أسرار. الأسرار تُخزَّن مشفّرة داخل قاعدة البيانات بمفتاح APP_ENCRYPTION_KEY فقط.',
  },
  {
    title: 'Gmail / Outlook بكلمة مرور IMAP',
    detail: 'غير مدعوم: جوجل ومايكروسوفت تتطلبان OAuth. استخدم مزودَي Gmail OAuth وOutlook OAuth أعلاه.',
  },
  {
    title: 'مزوّدات SMS أخرى (إلى جانب Twilio)',
    detail: 'غير مفعّلة حتى تُضاف محوّلات حقيقية لها. Twilio هو المزود المدعوم حالياً.',
  },
  {
    title: 'المزودات التجريبية (mock_llm / mock_voice)',
    detail: 'للتطوير والاختبارات فقط؛ العامل يرفضها تماماً عندما ENVIRONMENT=production.',
  },
];

const STATUS_META: Record<ConnectionStatus, { label: string; className: string }> = {
  unconfigured: { label: 'غير مضبوط', className: 'staff-disabled' },
  configured: { label: 'مضبوط ولم يُختبر', className: 'staff-pending' },
  connected: { label: 'متصل', className: 'staff-active' },
  failed: { label: 'فشل', className: 'staff-failed' },
};

const FAMILY_LABEL: Record<ProviderSpec['family'], string> = {
  email: 'البريد الإلكتروني',
  messaging: 'المراسلة (واتساب / SMS)',
  ai: 'الذكاء الاصطناعي',
  voice: 'المكالمات الصوتية',
  security: 'فحص الملفات',
};

function maskHint(key: string): string {
  return `محفوظ مشفّراً (${key}) — اتركه فارغاً للإبقاء عليه`;
}

export interface IntegrationsSettingsProps {
  companies: CompanyOption[];
}

export function IntegrationsSettings({ companies }: IntegrationsSettingsProps) {
  const [rows, setRows] = useState<IntegrationConnectionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [editor, setEditor] = useState<{ provider: ProviderSpec; company_id: string | null; pickCompany: boolean } | null>(null);
  const [busyKey, setBusyKey] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setRows(await api<IntegrationConnectionRecord[]>('/api/staff/admin/integration-connections'));
    } catch (cause) {
      setRows([]);
      setError(cause instanceof Error ? cause.message : 'تعذر تحميل إعدادات التكاملات.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) void load(); });
    return () => { active = false; };
  }, [load]);

  const byKey = useMemo(() => {
    const map = new Map<string, IntegrationConnectionRecord>();
    for (const row of rows) map.set(`${row.provider}:${row.company_id ?? 'global'}`, row);
    return map;
  }, [rows]);

  const runTest = async (provider: string, companyId: string | null) => {
    const key = `test:${provider}:${companyId ?? 'global'}`;
    setBusyKey(key);
    setError('');
    try {
      const result = await api<{ connection_status: ConnectionStatus; message: string; error_code: string | null }>(
        '/api/staff/admin/integration-connections/test',
        { method: 'POST', body: jsonBody({ provider, company_id: companyId }) },
      );
      setToast(result.message);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'فشل اختبار الاتصال.');
    } finally {
      setBusyKey('');
    }
  };

  const runSandboxSend = async (provider: string, companyId: string | null) => {
    const key = `sandbox:${provider}:${companyId ?? 'global'}`;
    setBusyKey(key);
    setError('');
    try {
      const result = await api<{ message: string; recipient_masked: string }>(
        '/api/staff/admin/integration-connections/sandbox-test',
        { method: 'POST', body: jsonBody({ provider, company_id: companyId, confirm: true }) },
      );
      setToast(`${result.message} (${result.recipient_masked})`);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'فشل إرسال رسالة الاختبار.');
    } finally {
      setBusyKey('');
    }
  };

  const startOAuth = async (provider: 'gmail_oauth' | 'outlook_oauth', companyId: string | null) => {
    setError('');
    try {
      const result = await api<{ authorization_url: string }>(
        `/api/staff/admin/integration-connections/oauth/${provider}/authorize`,
        { method: 'POST', body: jsonBody({ provider, company_id: companyId, redirect_path: '/staff/operations' }) },
      );
      window.location.assign(result.authorization_url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'تعذر بدء ربط الحساب.');
    }
  };

  const companyLabel = (id: string | null) =>
    id ? companies.find((company) => company.id === id)?.name_ar ?? id.slice(0, 8) : 'عام · جميع الشركات';

  return (
    <div className="operations-page">
      <SectionTitle
        title="تكاملات المزودين الفعلية"
        description="إعداد بيانات الاتصال الحقيقية لكل مزود، واختبار الاتصال قبل الاعتماد على الإرسال."
      />
      <div className="inline-alert info">
        <Icon name="lock" size={16} /> الأسرار تُخزَّن مشفّرة (APP_ENCRYPTION_KEY) ولا تُعرض بعد الحفظ ولا تُسجَّل في السجلات.
        اختبار الاتصال لا يرسل أي رسالة؛ إرسال الاختبار يتم فقط إلى عنوان sandbox وبموافقة صريحة.
      </div>
      {toast && (
        <div className="inline-alert success" role="status">
          {toast}
          <button className="notice-close" type="button" onClick={() => setToast('')} aria-label="إخفاء">
            <Icon name="close" size={14} />
          </button>
        </div>
      )}
      {error && (
        <div className="inline-alert error" role="alert">
          {error}
          <button type="button" onClick={() => void load()}>إعادة المحاولة</button>
        </div>
      )}

      {(['email', 'messaging', 'ai', 'voice', 'security'] as const).map((family) => (
        <div className="surface-card integration-family" key={family}>
          <h3 className="integration-family-title">{FAMILY_LABEL[family]}</h3>
          <div className="integration-grid">
            {PROVIDERS.filter((provider) => provider.family === family).map((provider) => {
              const record = byKey.get(`${provider.id}:global`);
              const companyConfigs = rows.filter((row) => row.provider === provider.id && row.company_id !== null);
              const status: ConnectionStatus = record?.connection_status ?? 'unconfigured';
              const meta = STATUS_META[status];
              return (
                <article className="integration-card" key={provider.id}>
                  <header className="integration-card-head">
                    <span className={`integration-logo${provider.family === 'messaging' ? ' integration-whatsapp' : ''}`}>
                      <Icon name={provider.icon} size={16} />
                    </span>
                    <div>
                      <strong>{provider.label}</strong>
                      <small>{provider.summary}</small>
                    </div>
                  </header>
                  {provider.scope !== 'company' && (
                    <div className="integration-scope-row">
                      <span className="scope-chip">عام · جميع الشركات</span>
                      <span className={`staff-status ${meta.className}`}><i />{meta.label}</span>
                    </div>
                  )}
                  {provider.scope !== 'company' && record && (
                    <small className="cell-secondary">
                      {record.last_test_at
                        ? `آخر اختبار: ${formatDateTime(record.last_test_at)}${record.last_error_code ? ` · ${record.last_error_code}` : ''}`
                        : record.has_credentials ? 'لم يُختبر بعد' : 'لا توجد بيانات محفوظة'}
                    </small>
                  )}
                  {companyConfigs.map((config) => (
                    <div className="integration-scope-row" key={config.id}>
                      <span className="scope-chip">{companyLabel(config.company_id)}</span>
                      <span className={`staff-status ${STATUS_META[config.connection_status].className}`}>
                        <i />{STATUS_META[config.connection_status].label}
                      </span>
                    </div>
                  ))}
                  <div className="row-action-group">
                    <button
                      className="button button-light button-small"
                      type="button"
                      onClick={() => setEditor({ provider, company_id: null, pickCompany: false })}
                    >
                      إعداد
                    </button>
                    {provider.scope === 'company' && (
                      <button
                        className="button button-light button-small"
                        type="button"
                        onClick={() => setEditor({ provider, company_id: null, pickCompany: true })}
                      >
                        إعداد شركة…
                      </button>
                    )}
                    <button
                      className="button button-dark button-small"
                      type="button"
                      disabled={busyKey === `test:${provider.id}:global` || !record?.has_credentials}
                      onClick={() => void runTest(provider.id, null)}
                    >
                      {busyKey.startsWith(`test:${provider.id}`) ? 'جارٍ…' : 'اختبار الاتصال'}
                    </button>
                    {['smtp', 'gmail_oauth', 'outlook_oauth', 'twilio_sms'].includes(provider.id) && (
                      <button
                        className="button button-quiet button-small"
                        type="button"
                        disabled={busyKey === `sandbox:${provider.id}:global` || !record?.has_credentials}
                        onClick={() => void runSandboxSend(provider.id, null)}
                      >
                        إرسال sandbox…
                      </button>
                    )}
                    {provider.oauth && (
                      <button
                        className="button button-quiet button-small"
                        type="button"
                        disabled={!record?.has_credentials}
                        onClick={() => void startOAuth(provider.oauth!, null)}
                      >
                        ربط حساب {provider.oauth === 'gmail_oauth' ? 'Google' : 'Microsoft'}
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      ))}

      <div className="surface-card integration-family">
        <h3 className="integration-family-title">ميزات غير مفعّلة</h3>
        <div className="integration-grid">
          {NOT_IMPLEMENTED.map((item) => (
            <article className="integration-card integration-card-disabled" key={item.title}>
              <header className="integration-card-head">
                <span className="integration-logo"><Icon name="lock" size={16} /></span>
                <div>
                  <strong>{item.title}</strong>
                  <small>{item.detail}</small>
                </div>
              </header>
              <span className="staff-status staff-disabled"><i />غير مفعّلة</span>
            </article>
          ))}
        </div>
      </div>

      {loading && (
        <div className="loading-surface compact-loading"><span className="loading-spinner" /> جارٍ تحميل الإعدادات…</div>
      )}

      {editor && (
        <IntegrationEditor
          key={`${editor.provider.id}:${editor.pickCompany ? 'pick' : editor.company_id ?? 'global'}`}
          provider={editor.provider}
          companyId={editor.company_id}
          companies={companies}
          pickCompany={editor.pickCompany}
          record={byKey.get(`${editor.provider.id}:${editor.pickCompany ? '' : editor.company_id ?? 'global'}`)}
          onClose={() => setEditor(null)}
          onSaved={async (message) => {
            setEditor(null);
            setToast(message);
            await load();
          }}
          onError={(message) => setError(message)}
          onOAuth={startOAuth}
        />
      )}
    </div>
  );
}

function IntegrationEditor({
  provider,
  companyId,
  companies,
  pickCompany,
  record,
  onClose,
  onSaved,
  onError,
  onOAuth,
}: {
  provider: ProviderSpec;
  companyId: string | null;
  companies: CompanyOption[];
  pickCompany: boolean;
  record?: IntegrationConnectionRecord;
  onClose: () => void;
  onSaved: (message: string) => Promise<void>;
  onError: (message: string) => void;
  onOAuth: (provider: 'gmail_oauth' | 'outlook_oauth', companyId: string | null) => Promise<void>;
}) {
  const [scopeCompany, setScopeCompany] = useState(companyId ?? '');
  const [enabled, setEnabled] = useState(record?.is_enabled ?? true);
  // Secret fields always start empty: the API never returns stored values.
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const field of provider.fields) initial[field.key] = field.boolean ? 'false' : '';
    return initial;
  });
  const [saving, setSaving] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    try {
      const credentials: Record<string, string> = {};
      for (const field of provider.fields) {
        const value = (values[field.key] ?? '').trim();
        if (value !== '') credentials[field.key] = field.boolean ? value : value;
      }
      const body = {
        provider: provider.id,
        company_id: (pickCompany ? scopeCompany : companyId) || null,
        enabled,
        clear_credentials: false,
        credentials: Object.keys(credentials).length ? credentials : undefined,
      };
      await api('/api/staff/admin/integration-connections', { method: 'PUT', body: jsonBody(body) });
      await onSaved('تم حفظ إعدادات الاتصال. البيانات المحفوظة لا تُعرض بعد الحفظ.');
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'تعذر حفظ الإعداد.');
    } finally {
      setSaving(false);
    }
  };

  const disconnect = async () => {
    setDisconnecting(true);
    try {
      await api('/api/staff/admin/integration-connections', {
        method: 'PUT',
        body: jsonBody({
          provider: provider.id,
          company_id: (pickCompany ? scopeCompany : companyId) || null,
          enabled: false,
          clear_credentials: true,
        }),
      });
      await onSaved('تم فصل المزود ومسح الأسرار المحفوظة.');
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'تعذر فصل المزود.');
    } finally {
      setDisconnecting(false);
    }
  };

  return (
    <Modal
      title={`إعداد ${provider.label}`}
      eyebrow={pickCompany ? 'إعداد خاص بشركة' : provider.scope === 'global' ? 'إعداد عام (كل الشركات)' : 'إعداد عام'}
      onClose={onClose}
      wide
    >
      <form onSubmit={(event) => void submit(event)}>
        <div className="operation-form-grid">
          {pickCompany && (
            <label className="field-label">
              شركة التأمين
              <select required value={scopeCompany} onChange={(event) => setScopeCompany(event.target.value)}>
                <option value="">اختر شركة…</option>
                {companies.map((company) => (
                  <option key={company.id} value={company.id}>{company.name_ar || company.name_en}</option>
                ))}
              </select>
            </label>
          )}
          {provider.fields.map((field) => (
            <label className="field-label" key={field.key}>
              {field.label}
              {field.boolean ? (
                <select
                  value={values[field.key] ?? 'false'}
                  onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))}
                >
                  <option value="true">مفعّل</option>
                  <option value="false">معطّل</option>
                </select>
              ) : (
                <input
                  type={field.secret ? 'password' : 'text'}
                  autoComplete={field.secret ? 'new-password' : 'off'}
                  value={values[field.key] ?? ''}
                  placeholder={field.placeholder ?? (field.secret ? maskHint(field.key) : '')}
                  onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))}
                  dir="ltr"
                />
              )}
              {field.help && <small>{field.help}</small>}
            </label>
          ))}
          <label className="switch-row">
            <span>
              <strong>تفعيل المزود</strong>
              <small>يجب اجتياز اختبار الاتصال قبل الاعتماد على الإرسال الفعلي.</small>
            </span>
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
            <i />
          </label>
          <div className="inline-alert warning full-form-row">
            لا تُرسل بيانات المزودين في المحادثات أو التذاكر. احفظ الأسرار هنا فقط؛ تُخزَّن مشفّرة ولا تُعاد للواجهة.
          </div>
        </div>
        <div className="modal-actions">
          <button className="button button-light" type="button" onClick={onClose}>إلغاء</button>
          {record?.has_credentials && (
            <button className="button button-quiet" type="button" disabled={disconnecting} onClick={() => void disconnect()}>
              {disconnecting ? 'جارٍ الفصل…' : 'فصل ومسح الأسرار'}
            </button>
          )}
          {provider.oauth && record?.has_credentials && (
            <button
              className="button button-quiet"
              type="button"
              onClick={() => void onOAuth(provider.oauth!, (pickCompany ? scopeCompany : companyId) || null)}
            >
              ربط حساب {provider.oauth === 'gmail_oauth' ? 'Google' : 'Microsoft'}
            </button>
          )}
          <button className="button button-dark" type="submit" disabled={saving}>
            {saving ? 'جارٍ الحفظ…' : 'حفظ الإعداد'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
