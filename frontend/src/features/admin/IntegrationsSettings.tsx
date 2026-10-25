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
import { Icon, type IconName } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { SkeletonRows, Stagger, StaggerItem } from '../../components/ui/Premium';
import { formatDateTime } from '../../utils/date';
import { validateIntegrationCredentials } from './integrationRules';
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
  /**
   * Mirrors the API rule: these providers can only be configured for one
   * company, so the console must not offer a platform-wide setup for them.
   * Saving one globally is rejected with 422 by the API.
   */
  companyRequired?: boolean;
  oauth?: 'gmail_oauth' | 'outlook_oauth';
  fields: FieldSpec[];
}

const PROVIDERS: ProviderSpec[] = [
  {
    id: 'smtp',
    label: 'بريد SMTP عام',
    icon: 'mail',
    family: 'email',
    summary: 'إرسال عبر خادم SMTP حقيقي مع TLS وعنوان مرسل موثّق. الاستقبال عبر IMAP منفصل.',
    scope: 'both',
    fields: [
      { key: 'host', label: 'خادم SMTP', placeholder: 'smtp.example.com' },
      { key: 'port', label: 'المنفذ', placeholder: '587' },
      { key: 'start_tls', label: 'STARTTLS (587)', boolean: true },
      { key: 'use_tls', label: 'TLS مباشر (465) — عطّل STARTTLS عند استخدامه', boolean: true },
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
    companyRequired: true,
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
    summary: 'تحليل الطلبات باقتراحات تُراجع بشرياً دائماً. بيانات الشخصيات تُخفّض قبل الإرسال.',
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

/**
 * The three ways a request reaches the brokerage. This is the whole settings
 * page as far as a non-technical operator is concerned: everything else is an
 * implementation detail of one of these three rows.
 */
interface ChannelSpec {
  id: 'whatsapp' | 'phone' | 'email';
  title: string;
  blurb: string;
  setupHint: string;
  chooseHint: string;
  icon: IconName;
  color: string;
  tint: string;
  /** Ordered by recommendation; the first is the default suggestion. */
  providers: string[];
}

const CHANNELS: ChannelSpec[] = [
  {
    id: 'whatsapp',
    title: 'واتساب',
    blurb: 'القناة الأساسية. يصل الطلب برسالة من العميل مع المرفقات، ويُفتح تلقائياً في قائمة الطلبات.',
    setupHint: 'يتطلب رقم واتساب للأعمال',
    chooseHint: 'اختر مزوّد واتساب الذي تستخدمه.',
    icon: 'message',
    color: '#128C7E',
    tint: 'rgba(18, 140, 126, 0.12)',
    providers: ['whatsapp_cloud'],
  },
  {
    id: 'phone',
    title: 'المكالمات الهاتفية',
    blurb: 'يُسجَّل الاتصال ويُحفظ مشفّراً، ويُنشأ طلب جديد باسم المتصل ورقمه دون إدخال يدوي.',
    setupHint: 'يتطلب رقماً من مزوّد اتصالات',
    chooseHint: 'اختر مزوّد الاتصالات الذي يستقبل مكالمات العملاء.',
    icon: 'phone',
    color: '#4A86D9',
    tint: 'rgba(74, 134, 217, 0.12)',
    providers: ['twilio', 'vonage'],
  },
  {
    id: 'email',
    title: 'البريد الإلكتروني',
    blurb: 'كل رسالة واردة تتحوّل إلى طلب، والردود من داخل المنصة تصل للعميل في نفس سلسلة البريد.',
    setupHint: 'يتطلب صندوق بريد للعمل',
    chooseHint: 'اختر طريقة الاتصال بصندوق البريد. Gmail وOutlook يتطلبان تسجيل دخول Google أو Microsoft.',
    icon: 'mail',
    /* Dusty rose, matching the email bar on the dashboard's intake breakdown
       — a channel must look the same everywhere it appears. It was amber,
       which spent the brand gold on an icon tile sitting next to a gold
       primary button. */
    color: '#C46A85',
    tint: 'rgba(196, 106, 133, 0.12)',
    providers: ['gmail_oauth', 'outlook_oauth', 'smtp', 'imap', 'sendgrid', 'ses'],
  },
];

/** Providers already surfaced by a channel card must not repeat under Advanced. */
const CHANNEL_PROVIDERS = new Set(CHANNELS.flatMap((channel) => channel.providers));

/** Providers whose API supports a real sandbox send. */
const SANDBOX_CAPABLE = new Set(['smtp', 'gmail_oauth', 'outlook_oauth', 'twilio_sms']);

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
  const [chooser, setChooser] = useState<ChannelSpec | null>(null);

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


  // ---------------------------------------------------------------- channels
  // The operator thinks in channels, not vendors: "is WhatsApp on?" rather
  // than "is whatsapp_cloud configured?". Each card owns the providers that
  // serve one channel and reports the best state among them.
  const channelState = (spec: ChannelSpec) => {
    const records = spec.providers
      .map((id) => ({ id, record: byKey.get(`${id}:global`) }))
      .filter((entry): entry is { id: string; record: IntegrationConnectionRecord } => Boolean(entry.record));
    const companyRecords = rows.filter((row) => spec.providers.includes(row.provider) && row.company_id !== null);
    const all = [...records.map((r) => r.record), ...companyRecords];
    const live = all.find((r) => r.connection_status === 'connected');
    const failed = all.find((r) => r.connection_status === 'failed');
    const ready = all.find((r) => r.connection_status === 'configured');
    const status: ConnectionStatus = live ? 'connected' : failed ? 'failed' : ready ? 'configured' : 'unconfigured';
    const active = live ?? failed ?? ready ?? all[0];
    const activeProvider = active
      ? PROVIDERS.find((candidate) => candidate.id === active.provider) ?? null
      : null;
    return {
      status,
      active,
      activeProvider,
      companyNames: companyRecords.map((row) => companyLabel(row.company_id)),
    };
  };

  const openChannel = (spec: ChannelSpec) => {
    const options = spec.providers
      .map((id) => PROVIDERS.find((provider) => provider.id === id))
      .filter((provider): provider is ProviderSpec => Boolean(provider));
    // One provider means one decision fewer: go straight to its form.
    if (options.length === 1) {
      const only = options[0];
      setEditor({ provider: only, company_id: null, pickCompany: only.scope === 'company' });
      return;
    }
    setChooser(spec);
  };

  return (
    <div className="ds-page intake-settings">
      <div className="ds-page-head">
        <div>
          <span className="ds-eyebrow">الإعدادات</span>
          <h1 className="ds-page-title">قنوات استقبال الطلبات</h1>
          <p className="ds-page-sub">
            ثلاث قنوات يصل منها الطلب. فعّل ما تحتاجه — الإعداد لا يستغرق أكثر من دقيقة، ويمكنك
            اختبار الاتصال في أي وقت دون إرسال أي رسالة للعملاء.
          </p>
        </div>
        <button className="ds-ghost-btn" type="button" onClick={() => void load()} disabled={loading}>
          <Icon name="refresh" size={15} />
          {loading ? 'جارٍ التحديث…' : 'تحديث'}
        </button>
      </div>

      {toast && (
        <div className="ds-banner ds-banner-success" role="status">
          <Icon name="check" size={16} />
          <span>{toast}</span>
          <button type="button" onClick={() => setToast('')} aria-label="إخفاء"><Icon name="close" size={14} /></button>
        </div>
      )}
      {error && (
        <div className="ds-banner ds-banner-error" role="alert">
          <Icon name="alert" size={16} />
          <span>{error}</span>
          <button type="button" onClick={() => void load()}>إعادة المحاولة</button>
        </div>
      )}

      <Stagger className="ds-channel-grid">
        {CHANNELS.map((spec) => {
          const { status, active, activeProvider, companyNames } = channelState(spec);
          const connected = status === 'connected';
          const failed = status === 'failed';
          const busy = busyKey.startsWith(`test:${active?.provider ?? '—'}`);
          return (
            <StaggerItem key={spec.id}>
              <article className={`ds-channel ds-card${connected ? ' is-connected' : ''}`} data-channel={spec.id}>
                <span className="ds-channel-glow" aria-hidden="true" />

                <header className="ds-channel-head">
                  <span className="ds-channel-logo" style={{ color: spec.color, background: spec.tint }}>
                    <Icon name={spec.icon} size={26} />
                  </span>
                  <span className={`ds-channel-state ds-channel-state-${connected ? 'on' : failed ? 'fail' : 'off'}`}>
                    <i />
                    {connected ? 'متصل' : failed ? 'يحتاج مراجعة' : status === 'configured' ? 'جاهز للاختبار' : 'غير متصل'}
                  </span>
                </header>

                <h2 className="ds-channel-name">{spec.title}</h2>
                <p className="ds-channel-copy">{spec.blurb}</p>

                <div className="ds-channel-meta">
                  {connected && active?.last_test_at && (
                    <span className="ds-tag"><Icon name="check" size={13} /> آخر اختبار {formatDateTime(active.last_test_at)}</span>
                  )}
                  {failed && active?.last_error_code && (
                    <span className="ds-tag ds-tag-danger"><Icon name="alert" size={13} /> {active.last_error_code}</span>
                  )}
                  {/* The vendor name is only meaningful once something is
                      actually stored; before that it is noise. */}
                  {activeProvider && active?.has_credentials && (
                    <span className="ds-tag">{activeProvider.label}</span>
                  )}
                  {companyNames.map((name) => <span className="ds-tag" key={name}>{name}</span>)}
                  {!active?.has_credentials && <span className="ds-tag">{spec.setupHint}</span>}
                </div>

                <div className="ds-channel-actions">
                  <button
                    className={connected ? 'ds-ghost-btn' : 'ds-gold-btn'}
                    type="button"
                    onClick={() => openChannel(spec)}
                  >
                    <Icon name={connected ? 'settings' : 'plug'} size={15} />
                    {connected ? 'إدارة' : 'ربط القناة'}
                  </button>
                  {active?.has_credentials && (
                    <button
                      className="ds-quiet-btn"
                      type="button"
                      disabled={busy}
                      onClick={() => void runTest(active.provider, active.company_id)}
                    >
                      {busy ? 'جارٍ الاختبار…' : 'اختبار الاتصال'}
                    </button>
                  )}
                </div>
              </article>
            </StaggerItem>
          );
        })}
      </Stagger>

      <p className="ds-reassure">
        <Icon name="lock" size={14} />
        البيانات تُحفظ مشفّرة ولا تظهر بعد الحفظ. اختبار الاتصال لا يُرسل أي رسالة للعملاء.
      </p>

      {/* Everything that is not one of the three intake channels lives behind
          this disclosure: still reachable, no longer in the way. */}
      <details className="ds-advanced">
        <summary>
          <span><Icon name="sliders" size={15} /> إعدادات متقدمة</span>
          <small>الذكاء الاصطناعي، الرسائل النصية، فحص الملفات</small>
          <Icon name="chevron" size={15} />
        </summary>
        <div className="ds-advanced-body">
          {(['messaging', 'ai', 'security'] as const).map((family) => {
            const items = PROVIDERS.filter(
              (provider) => provider.family === family && !CHANNEL_PROVIDERS.has(provider.id),
            );
            if (!items.length) return null;
            return (
              <section className="ds-advanced-group" key={family}>
                <h3>{FAMILY_LABEL[family]}</h3>
                <div className="ds-advanced-grid">
                  {items.map((provider) => {
                    const record = byKey.get(`${provider.id}:global`);
                    const meta = STATUS_META[record?.connection_status ?? 'unconfigured'];
                    return (
                      <article className="ds-mini-card" key={provider.id}>
                        <div className="ds-mini-head">
                          <span className="ds-mini-logo"><Icon name={provider.icon} size={15} /></span>
                          <strong>{provider.label}</strong>
                          <span className={`staff-status ${meta.className}`}><i />{meta.label}</span>
                        </div>
                        <p>{provider.summary}</p>
                        <div className="ds-mini-actions">
                          {!provider.companyRequired && (
                            <button className="ds-quiet-btn" type="button" onClick={() => setEditor({ provider, company_id: null, pickCompany: false })}>إعداد</button>
                          )}
                          {provider.scope === 'company' && (
                            <button className="ds-quiet-btn" type="button" onClick={() => setEditor({ provider, company_id: null, pickCompany: true })}>إعداد شركة…</button>
                          )}
                          <button
                            className="ds-quiet-btn"
                            type="button"
                            disabled={busyKey.startsWith(`test:${provider.id}`) || !record?.has_credentials}
                            onClick={() => void runTest(provider.id, null)}
                          >
                            {busyKey.startsWith(`test:${provider.id}`) ? 'جارٍ…' : 'اختبار'}
                          </button>
                          {SANDBOX_CAPABLE.has(provider.id) && (
                            <button
                              className="ds-quiet-btn"
                              type="button"
                              disabled={busyKey === `sandbox:${provider.id}:global` || !record?.has_credentials}
                              onClick={() => void runSandboxSend(provider.id, null)}
                            >
                              إرسال sandbox…
                            </button>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              </section>
            );
          })}

          <section className="ds-advanced-group">
            <h3>ميزات غير مفعّلة</h3>
            <ul className="ds-unavailable">
              {NOT_IMPLEMENTED.map((item) => (
                <li key={item.title}>
                  <Icon name="lock" size={14} />
                  <div><strong>{item.title}</strong><span>{item.detail}</span></div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </details>

      {loading && rows.length === 0 && <SkeletonRows rows={3} />}

      {chooser && (
        <Modal title={`ربط ${chooser.title}`} onClose={() => setChooser(null)}>
          <p className="ds-chooser-lead">{chooser.chooseHint}</p>
          <div className="ds-chooser">
            {chooser.providers.map((id) => {
              const provider = PROVIDERS.find((candidate) => candidate.id === id);
              if (!provider) return null;
              const record = byKey.get(`${provider.id}:global`);
              const meta = STATUS_META[record?.connection_status ?? 'unconfigured'];
              return (
                <button
                  className="ds-chooser-option"
                  type="button"
                  key={provider.id}
                  onClick={() => {
                    setChooser(null);
                    setEditor({ provider, company_id: null, pickCompany: provider.scope === 'company' });
                  }}
                >
                  <span className="ds-mini-logo"><Icon name={provider.icon} size={16} /></span>
                  <span className="ds-chooser-text">
                    <strong>{provider.label}</strong>
                    <small>{provider.summary}</small>
                  </span>
                  <span className={`staff-status ${meta.className}`}><i />{meta.label}</span>
                  <Icon name="arrow" size={15} />
                </button>
              );
            })}
          </div>
        </Modal>
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
  const [fieldIssues, setFieldIssues] = useState<Record<string, string>>({});

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    try {
      const credentials: Record<string, string> = {};
      for (const field of provider.fields) {
        const value = (values[field.key] ?? '').trim();
        if (value !== '') credentials[field.key] = field.boolean ? value : value;
      }
      // The API rejects an invalid credential set with 422; check the same
      // rules here so the operator sees which field is wrong.
      const issues = validateIntegrationCredentials(provider.id, credentials);
      if (issues.length) {
        setFieldIssues(Object.fromEntries(issues.map((issue) => [issue.field, issue.message])));
        throw new Error(issues[0].message);
      }
      setFieldIssues({});
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
              {fieldIssues[field.key] && <small role="alert" className="field-error">{fieldIssues[field.key]}</small>}
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
