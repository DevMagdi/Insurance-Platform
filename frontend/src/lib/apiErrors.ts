/**
 * Turn an API error body into a message a person can act on.
 *
 * FastAPI answers a validation failure (422) with a *list* of issues. The
 * clients used to keep only string details, so every validation failure was
 * shown as "Request failed (422)" -- the user had no idea which field was
 * wrong. Server details are also English while the product is Arabic.
 *
 * This module is the single place that converts a raw `detail` into the
 * Arabic text shown in the UI. The raw server value is kept separately for
 * diagnostics; nothing in the UI compares the translated text.
 */

/** Human labels for the request fields users can actually fix. */
const FIELD_LABELS: Record<string, string> = {
  email: 'البريد الإلكتروني',
  full_name: 'الاسم الكامل',
  name: 'الاسم',
  name_en: 'الاسم بالإنجليزية',
  name_ar: 'الاسم بالعربية',
  title: 'العنوان',
  description: 'الوصف',
  subject: 'عنوان الشكوى',
  body: 'النص',
  body_en: 'النص بالإنجليزية',
  body_ar: 'النص بالعربية',
  national_id: 'الرقم القومي',
  mobile: 'رقم الموبايل',
  emergency_phone: 'هاتف الطوارئ',
  address: 'العنوان',
  city: 'المدينة',
  job_title: 'المسمى الوظيفي',
  password: 'كلمة المرور',
  code: 'الرمز',
  token: 'الرابط',
  policy_number: 'رقم الوثيقة',
  company_id: 'شركة التأمين',
  company_ids: 'الشركات',
  insurance_type: 'نوع التأمين',
  insurance_types: 'أنواع التأمين',
  supported_types: 'أنواع التأمين المدعومة',
  department_id: 'القسم',
  category_id: 'التصنيف',
  priority: 'الأولوية',
  status: 'الحالة',
  slug: 'المعرّف المختصر',
  contact_emails: 'عناوين التواصل',
  recipient_emails: 'المستلمون',
  cron_expression: 'جدول التكرار',
  time_zone: 'المنطقة الزمنية',
  window_days: 'نافذة البيانات',
  response_minutes: 'زمن الاستجابة',
  resolution_minutes: 'زمن الحل',
  member_ids: 'أعضاء الفريق',
  lead_staff_id: 'قائد الفريق',
  target_team_id: 'الفريق المستهدف',
  target_agent_id: 'الموظف المستهدف',
  platform_name: 'اسم المنصة',
  logo_url: 'مسار الشعار',
  ticket_no: 'رقم الشكوى',
  reason: 'السبب',
  date_from: 'تاريخ البداية',
  date_to: 'تاريخ النهاية',
  search: 'البحث',
  permissions: 'الصلاحيات',
  ownership: 'مستوى الملكية',
  role_ids: 'الأدوار',
  url: 'الرابط',
  event_types: 'أنواع الأحداث',
  credentials: 'بيانات الاتصال',
  score: 'التقييم',
  comment: 'التعليق',
  files: 'الملفات',
  filename: 'اسم الملف',
};

/** Arabic text for the validation issue types FastAPI/Pydantic report. */
const ISSUE_TEXT: Record<string, string> = {
  missing: 'حقل مطلوب',
  string_too_short: 'النص أقصر من المسموح',
  string_too_long: 'النص أطول من المسموح',
  string_pattern_mismatch: 'الصيغة غير صحيحة',
  string_type: 'يجب أن يكون نصاً',
  enum: 'قيمة غير مسموحة',
  literal_error: 'قيمة غير مسموحة',
  uuid_type: 'اختيار غير صالح',
  uuid_parsing: 'اختيار غير صالح',
  extra_forbidden: 'حقل غير متوقع',
  int_parsing: 'يجب أن يكون رقماً صحيحاً',
  int_type: 'يجب أن يكون رقماً صحيحاً',
  greater_than: 'القيمة أصغر من المسموح',
  greater_than_equal: 'القيمة أصغر من المسموح',
  less_than: 'القيمة أكبر من المسموح',
  less_than_equal: 'القيمة أكبر من المسموح',
  too_short: 'عدد العناصر أقل من المطلوب',
  too_long: 'عدد العناصر أكبر من المسموح',
  bool_parsing: 'قيمة غير صالحة',
  datetime_parsing: 'تاريخ غير صالح',
  datetime_from_date_parsing: 'تاريخ غير صالح',
  timezone_aware: 'التاريخ يحتاج منطقة زمنية',
  list_type: 'يجب أن تكون قائمة',
  json_invalid: 'بيانات الطلب غير صالحة',
};

/** Arabic text for the server details a user can meet in normal use. */
const DETAIL_TEXT: Record<string, string> = {
  'Invalid credentials': 'البريد الإلكتروني أو كلمة المرور غير صحيحة.',
  'Invalid or expired code': 'الرمز غير صحيح أو انتهت صلاحيته.',
  'We could not send your verification code right now. Please try again in a few minutes.':
    'لم نتمكن من إرسال رمز التحقق الآن. حاول مرة أخرى بعد بضع دقائق.',
  'Too many failed attempts; try again later': 'محاولات فاشلة كثيرة. حاول مرة أخرى لاحقاً.',
  'Temporarily blocked; try later': 'تم الإيقاف مؤقتاً بسبب كثرة الطلبات. حاول لاحقاً.',
  'Permission denied': 'ليست لديك صلاحية لتنفيذ هذا الإجراء.',
  'Session expired or access changed': 'انتهت الجلسة أو تغيّرت صلاحياتك. سجّل الدخول مجدداً.',
  'Authentication required': 'سجّل الدخول للمتابعة.',
  'Name or email is already in use': 'الاسم أو البريد الإلكتروني مستخدم بالفعل.',
  'Platform Super Admin access required': 'تغيير هوية المنصة متاح لمالك المنصة فقط.',
  'Global administration scope required': 'هذا الإعداد يتطلب نطاق إدارة عاماً لكل الشركات.',
  'Cannot remove the last Super Admin': 'لا يمكن إزالة آخر مدير عام للمنصة.',
  'Invalid access specification': 'إعدادات الوصول غير صالحة.',
  'Access change forbidden': 'غير مسموح بتعديل هذه الصلاحيات.',
  'Cannot edit your own admin profile': 'لا يمكنك تعديل ملفك الإداري بنفسك.',
  'Upload a PNG, JPEG or WebP logo': 'ارفع شعاراً بصيغة PNG أو JPEG أو WebP.',
  'Logo must be no larger than 2 MB': 'حجم الشعار يجب ألا يتجاوز 2 ميجابايت.',
  'Logo content does not match its image type': 'محتوى الشعار لا يطابق صيغة الصورة.',
  'Complete your personal details and upload a profile photo': 'أكمل بياناتك الشخصية وارفع صورة شخصية.',
  'Profile identity is already registered': 'الرقم القومي أو الهاتف مسجل لحساب آخر.',
  'Invalid or expired invitation': 'رابط الدعوة غير صالح أو انتهت صلاحيته.',
  'Provide national ID and name to register': 'أدخل الاسم والرقم القومي لإكمال التسجيل.',
  'Narrow filters to 1000 complaints or fewer':
    'النتائج تتجاوز 1000 شكوى. ضيّق الفلاتر، أو أنشئ ملفاً كاملاً من صفحة التصدير.',
  'A start and end date are required': 'حدّد تاريخ البداية والنهاية.',
  'Limit analytics to 366 days': 'الحد الأقصى للفترة 366 يوماً.',
  'Feedback is unavailable': 'التقييم غير متاح لهذه الشكوى حالياً.',
  'Complaint cannot be reopened': 'لا يمكن إعادة فتح هذه الشكوى حالياً.',
  'Identity cannot be registered':
    'لا يمكن تسجيل هذه الهوية: الرقم القومي مسجّل بالفعل. إذا غيّرت رقم الموبايل أو البريد الإلكتروني، تواصل مع الدعم لتحديث حسابك.',
  'Service temporarily unavailable; please retry shortly.':
    'الخدمة غير متاحة مؤقتاً. حاول مرة أخرى بعد قليل.',
  'Escalation is unavailable': 'التصعيد غير متاح لهذه الشكوى حالياً.',
  'Report schedule unavailable': 'التقرير المجدول غير متاح.',
  'Staff user unavailable': 'الموظف غير موجود أو خارج نطاقك.',
  'Assignment preview not permitted': 'معاينة التوزيع غير مسموحة لنطاقك.',
  'Ticket and national ID pair not found': 'رقم الشكوى أو الرقم القومي غير صحيح.',
  'Complaint unavailable': 'الشكوى غير متاحة أو خارج نطاقك.',
  'Complaint not found': 'الشكوى غير موجودة.',
  'Notification not found': 'التنبيه غير موجود.',
  'Notification unavailable': 'التنبيه غير متاح.',
  'Department unavailable': 'القسم غير متاح.',
  'Department name already exists': 'اسم القسم موجود بالفعل.',
  'Team not found': 'الفريق غير موجود.',
  'Team could not be created': 'تعذر إنشاء الفريق.',
  'Invalid or revoked API key': 'مفتاح الواجهة غير صالح أو تم إلغاؤه.',
  'Invalid API key': 'مفتاح الواجهة غير صالح.',
  'API key required': 'مفتاح الواجهة مطلوب.',
  'API key lacks complaints.view': 'مفتاح الواجهة لا يسمح بعرض الشكاوى.',
  'Webhook payload is too large': 'حجم بيانات التكامل أكبر من المسموح.',
  'Storage inventory is temporarily unavailable': 'جرد المخزن غير متاح مؤقتاً. حاول مرة أخرى.',
  'Invalid storage cursor': 'رمز المتابعة غير صالح.',
  'Invalid storage prefix': 'مسار التخزين غير صالح.',
  'Unknown storage prefix': 'مسار التخزين غير معروف.',
  'Invalid limit': 'الحد المطلوب غير صالح.',
  'Privacy request unavailable': 'طلب الخصوصية غير متاح.',
  'Privacy request cannot be created': 'تعذر إنشاء طلب الخصوصية.',
  'Invalid customer profile': 'بيانات الملف الشخصي غير صالحة.',
  'Consent cannot be recorded': 'تعذر تسجيل الموافقة.',
  'Customer unavailable': 'حساب العميل غير متاح.',
  'Completed export unavailable': 'ملف التصدير غير متاح.',
  'Private export artifact unavailable': 'ملف التصدير الخاص غير متاح.',
  'Customer identity does not match sign-in': 'هوية العميل لا تطابق تسجيل الدخول.',
  'Session not found': 'الجلسة غير موجودة.',
  'Session expired; sign in again': 'انتهت الجلسة. سجّل الدخول مجدداً.',
};

/** Arabic text for the `Value error, ...` messages of the API validators. */
const VALUE_ERROR_TEXT: Record<string, string> = {
  'Team lead must be included in the members': 'قائد الفريق يجب أن يكون من أعضاء الفريق.',
  'Duplicate team members': 'يوجد عضو مكرر في الفريق.',
  'A rule needs a target team, a target agent, or both': 'القاعدة تحتاج فريقاً أو موظفاً مستهدفاً.',
  'Resolution target cannot be shorter than the response target': 'زمن الحل يجب ألا يقل عن زمن الاستجابة.',
  'Contact emails must be lowercase addresses': 'عناوين التواصل يجب أن تكون عناوين بريد صحيحة.',
  'Recipient addresses must be unique': 'عناوين المستلمين يجب ألا تتكرر.',
  'Invalid recipient address': 'يوجد عنوان مستلم غير صالح.',
  'At least one recipient is required': 'أدخل مستلماً واحداً على الأقل.',
  'Unknown IANA time zone': 'المنطقة الزمنية غير معروفة.',
  'Logo must be a same-origin static asset path': 'مسار الشعار يجب أن يكون مساراً محلياً يبدأ بـ /.',
  'Select all_companies or at least one company ID, not both': 'اختر «جميع الشركات» أو شركة واحدة على الأقل.',
  'An override cannot both allow and deny': 'لا يمكن السماح بالصلاحية ومنعها في الوقت نفسه.',
  'Unknown permission code': 'توجد صلاحية غير معروفة.',
  'date_from must not follow date_to': 'تاريخ البداية يجب أن يسبق تاريخ النهاية.',
  'An export requires a date range of at most 366 days': 'التصدير يحتاج فترة لا تتجاوز 366 يوماً.',
  'Scheduled reports cannot pin absolute dates': 'التقارير المجدولة تستخدم فترة متحركة وليس تواريخ ثابتة.',
  'National ID contains an invalid birth date': 'الرقم القومي يحتوي على تاريخ ميلاد غير صحيح.',
  'National ID must contain exactly 14 ASCII digits': 'الرقم القومي يجب أن يتكون من 14 رقماً.',
  'National ID has an invalid birth century': 'الرقم القومي غير صحيح.',
  'National ID birth date cannot be in the future': 'تاريخ الميلاد في الرقم القومي لا يمكن أن يكون في المستقبل.',
  'National ID contains an unknown governorate code': 'كود المحافظة في الرقم القومي غير صحيح.',
  'This integration must be configured for a specific company': 'هذا التكامل يجب ضبطه لشركة محددة.',
  'Provider port must be between 1 and 65535': 'المنفذ يجب أن يكون بين 1 و 65535.',
  'Choose STARTTLS or implicit TLS, not both': 'اختر STARTTLS أو TLS المباشر، وليس الاثنين.',
  'Provider sender email is invalid': 'عنوان المرسل غير صالح.',
  'Provider endpoint must use HTTPS': 'الرابط الأساسي يجب أن يبدأ بـ https://',
  'Status callback must be a public HTTPS URL': 'رابط استقبال الأحداث يجب أن يكون HTTPS عاماً.',
  'Filename must not contain paths or controls': 'اسم الملف يحتوي على مسارات غير مسموحة.',
  'Combined uploads exceed the 25 MB limit': 'مجموع الملفات يتجاوز 25 ميجابايت.',
};

interface ValidationIssue {
  type?: unknown;
  loc?: unknown;
  msg?: unknown;
}

function fieldName(loc: unknown): string | null {
  if (!Array.isArray(loc)) return null;
  // `loc` looks like ["body", "profile", "national_id"]: the last string that
  // is not the request part is the field the user typed.
  for (let index = loc.length - 1; index >= 0; index -= 1) {
    const part = loc[index];
    if (typeof part === 'string' && !['body', 'query', 'path', 'header'].includes(part)) return part;
  }
  return null;
}

function describeIssue(issue: ValidationIssue): string {
  const field = fieldName(issue.loc);
  const label = field ? FIELD_LABELS[field] ?? field : null;
  const message = typeof issue.msg === 'string' ? issue.msg : '';
  const valueError = message.startsWith('Value error, ') ? message.slice('Value error, '.length) : null;
  if (valueError) {
    const text = VALUE_ERROR_TEXT[valueError] ?? valueError;
    return label ? `${label}: ${text}` : text;
  }
  if (typeof issue.type === 'string' && issue.type === 'missing' && field && /^(idempotency-key|x-)/i.test(field)) {
    // A missing header is a client defect, never something the user can fix.
    return 'تعذر إرسال الطلب. حدّث الصفحة وحاول مرة أخرى.';
  }
  const text = (typeof issue.type === 'string' && ISSUE_TEXT[issue.type]) || 'قيمة غير صالحة';
  return label ? `${label}: ${text}` : text;
}

/** Generic Arabic text per HTTP status when the server gave no usable detail. */
function statusText(status: number): string {
  if (status === 400) return 'تعذر تنفيذ الطلب. راجع البيانات وحاول مرة أخرى.';
  if (status === 401) return 'سجّل الدخول للمتابعة.';
  if (status === 403) return 'ليست لديك صلاحية لتنفيذ هذا الإجراء.';
  if (status === 404) return 'العنصر المطلوب غير موجود أو خارج نطاقك.';
  if (status === 409) return 'تعارض مع بيانات موجودة. حدّث الصفحة وحاول مرة أخرى.';
  if (status === 413) return 'حجم الطلب أكبر من المسموح.';
  if (status === 415) return 'صيغة الملف غير مدعومة.';
  if (status === 422) return 'بعض البيانات غير صالحة. راجع الحقول وحاول مرة أخرى.';
  if (status === 429) return 'طلبات كثيرة خلال وقت قصير. انتظر قليلاً ثم حاول مجدداً.';
  if (status >= 500) return 'حدث خطأ في الخادم. حاول مرة أخرى بعد قليل.';
  return `تعذر تنفيذ الطلب (${status}).`;
}

/**
 * The Arabic message for an API error.
 *
 * @param status HTTP status of the response.
 * @param detail The `detail` member of the JSON error body (string or list).
 */
export function describeApiError(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail.trim()) {
    return DETAIL_TEXT[detail] ?? VALUE_ERROR_TEXT[detail] ?? detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const issues = detail.filter((item): item is ValidationIssue => Boolean(item) && typeof item === 'object');
    if (issues.length > 0) {
      const first = describeIssue(issues[0]);
      return issues.length > 1 ? `${first} (و${issues.length - 1} ملاحظة أخرى)` : first;
    }
  }
  return statusText(status);
}
