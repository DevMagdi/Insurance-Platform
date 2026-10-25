import type { Permission } from '../../types';

export const PERMISSION_LABELS: Record<Permission, string> = {
  'complaints.view': 'عرض الطلبات', 'complaints.create': 'إنشاء طلب', 'complaints.edit': 'تعديل بيانات الطلب',
  'complaints.assign': 'إسناد الطلبات', 'complaints.reassign': 'إعادة الإسناد', 'complaints.change_status': 'تغيير الحالة',
  'complaints.reply_public': 'إرسال رد علني', 'complaints.add_internal_note': 'إضافة ملاحظة داخلية', 'complaints.merge': 'دمج الطلبات',
  'complaints.escalate': 'تحويل الطلب', 'complaints.delete': 'حذف الطلبات', 'complaints.export': 'تصدير الطلبات',
  'complaints.view_attachments': 'عرض المرفقات', 'complaints.view_full_national_id': 'عرض رقم الهوية كاملاً',
  'analytics.view': 'عرض التحليلات', 'analytics.export': 'تصدير التحليلات', 'users.view': 'عرض المستخدمين',
  'users.create': 'دعوة مستخدم', 'users.edit': 'تعديل بيانات المستخدم', 'users.deactivate': 'تعطيل المستخدم',
  'users.manage_access': 'إدارة الوصول والنطاقات', 'roles.manage': 'إدارة الأدوار', 'teams.manage': 'إدارة الفرق', 'companies.manage': 'إدارة شركات التأمين',
  'categories.manage': 'إدارة التصنيفات', 'sla.manage': 'إدارة قواعد SLA', 'templates.manage': 'إدارة الردود الجاهزة',
  'integrations.manage': 'إدارة التكاملات', 'integrations.send_email': 'إرسال البريد للشركات',
  'integrations.send_whatsapp': 'إرسال واتساب', 'integrations.ai_call': 'الاتصال الصوتي الذكي',
  'reports.manage': 'إدارة التقارير المجدولة', 'audit.view': 'عرض سجل التدقيق', 'settings.manage': 'إدارة إعدادات النظام', 'storage.manage': 'استعراض مخزن الملفات',
  'crm.view': 'عرض المبيعات والعملاء المحتملين', 'crm.manage': 'إدارة العملاء المحتملين والمتابعات', 'crm.convert': 'تحويل العميل المحتمل إلى عميل',
  'policies.view': 'عرض الوثائق والأقساط', 'policies.manage': 'إدارة الوثائق وتسويات العمولة',
  'issuing.view': 'عرض ملفات الإصدار', 'issuing.manage': 'إدارة إصدار الوثائق وقوائم الأوراق',
  'imports.manage': 'استيراد بيانات من Excel', 'payments.manage': 'إدارة مدفوعات الأقساط',
};
