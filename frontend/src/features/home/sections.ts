import type { AppRoute } from '../../types';

/**
 * Card OS section metadata: each work area owns exactly one glow colour
 * (design-system.css, --glow-*). The glow is decoration only — it tints
 * the section's own card, rail dock and sub-card strip, never primary
 * actions (those stay coral) and never text.
 */
export type GlowKey = 'requests' | 'channels' | 'metrics' | 'documents' | 'crm' | 'admin';

export const SECTION_GLOW: Partial<Record<AppRoute['page'], GlowKey>> = {
  complaints: 'requests',
  ticket: 'requests',
  channels: 'channels',
  overview: 'metrics',
  'insurance-ops': 'documents',
  'crm-module': 'crm',
  admin: 'admin',
  users: 'admin',
  customers: 'admin',
  roles: 'admin',
  operations: 'admin',
  audit: 'admin',
  storage: 'admin',
};

export const SECTION_DESCRIPTION: Partial<Record<AppRoute['page'], string>> = {
  complaints: 'صندوق الطلبات والشكاوى ومتابعة المحادثات حتى الحل',
  channels: 'واتساب والمكالمات والبريد — القنوات التي تصل عبرها الطلبات',
  overview: 'مؤشرات الأداء والالتزام بمواعيد الحل (SLA)',
  'insurance-ops': 'عروض الأسعار والإصدار والوثائق والمدفوعات',
  'crm-module': 'المبيعات: leads و pipeline والمهام والتحويل',
  admin: 'إدارة المنصة والتقارير التشغيلية',
  users: 'حسابات فريق العمل وعضويات الشركات',
  customers: 'حسابات العملاء المسجلين',
  roles: 'الأدوار وصلاحيات الوصول',
  operations: 'الشركات والكتالوج واتفاقيات مستوى الخدمة والقوالب',
  audit: 'سجل الإجراءات والحوكمة',
  storage: 'التخزين الداخلي والملفات',
};
