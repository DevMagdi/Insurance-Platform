/**
 * Client-side mirrors of the server validators (Egyptian national ID,
 * `+201…` mobile, complaint payload). The server remains authoritative;
 * these exist for live, per-field feedback in the wizard.
 */
import { z } from 'zod';

export type NationalIdError = 'length' | 'century' | 'date' | 'future' | 'governorate';

const GOVERNORATES = new Set(
  [...Array.from({ length: 35 }, (_, i) => String(i + 1).padStart(2, '0')), '88'],
);

/** Port of `backend/app/security/national_id.py`. Returns an error code or null. */
export function nationalIdError(value: string): NationalIdError | null {
  const digits = value.replace(/[\s-]/g, '');
  if (!/^[0-9]{14}$/.test(digits)) return 'length';
  if (digits[0] !== '2' && digits[0] !== '3') return 'century';
  const century = digits[0] === '2' ? 1900 : 2000;
  const year = century + Number(digits.slice(1, 3));
  const month = Number(digits.slice(3, 5));
  const day = Number(digits.slice(5, 7));
  const born = new Date(Date.UTC(year, month - 1, day));
  if (
    born.getUTCFullYear() !== year ||
    born.getUTCMonth() !== month - 1 ||
    born.getUTCDate() !== day
  ) {
    return 'date';
  }
  const today = new Date();
  const todayUtc = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  if (born.getTime() > todayUtc) return 'future';
  if (!GOVERNORATES.has(digits.slice(7, 9))) return 'governorate';
  return null;
}

export function isValidNationalId(value: string): boolean {
  return nationalIdError(value) === null;
}

/** `•••• •••• ••12 34` — only the last four digits are ever shown. */
export function maskNationalId(value: string): string {
  const digits = value.replace(/\D/g, '');
  if (digits.length !== 14) return digits;
  return `•••• •••• ••${digits.slice(10, 12)} ${digits.slice(12)}`;
}

export function formatNationalId(value: string): string {
  const digits = value.replace(/\D/g, '').slice(0, 14);
  return digits.replace(/(\d{4})(?=\d)/g, '$1 ').trim();
}

/** Accept `01xxxxxxxxx`, `201xxxxxxxxx` or `+201xxxxxxxxx`; return E.164-like form. */
export function normalizeMobile(input: string): string {
  const compact = input.replace(/[\s-]/g, '');
  if (/^01\d{9}$/.test(compact)) return `+20${compact.slice(1)}`;
  if (/^201\d{9}$/.test(compact)) return `+${compact}`;
  return compact;
}

export function isValidMobile(input: string): boolean {
  return /^\+201\d{9}$/.test(normalizeMobile(input));
}

export const OTP_PATTERN = /^[0-9]{6}$/;
export const TICKET_PATTERN = /^CMP-\d{4}-\d{6,}$/;

export function isValidTicketNo(value: string): boolean {
  return TICKET_PATTERN.test(value.trim());
}

const nationalIdSchema = z
  .string()
  .transform((v) => v.replace(/[\s-]/g, ''))
  .refine((v) => nationalIdError(v) === null, { message: 'validation.nationalId' });

const mobileSchema = z
  .string()
  .transform(normalizeMobile)
  .refine((v) => /^\+201\d{9}$/.test(v), { message: 'validation.mobile' });

export const identityStepSchema = z.object({
  full_name: z.string().trim().min(2, 'validation.fullName').max(120),
  national_id: nationalIdSchema,
  mobile: mobileSchema,
  whatsapp_available: z.boolean(),
  email: z
    .string()
    .trim()
    .max(254)
    .refine((v) => v === '' || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v), {
      message: 'validation.email',
    }),
});

export const policyStepSchema = z.object({
  company_id: z.string().uuid('validation.company'),
  insurance_type: z.enum(['health', 'general', 'both'], { message: 'validation.insuranceType' }),
  policy_number: z
    .string()
    .trim()
    .min(5, 'validation.policyNumber')
    .max(60)
    .regex(/^[A-Za-z0-9][A-Za-z0-9/_-]*$/, 'validation.policyNumber'),
});

export const complaintStepSchema = z.object({
  subject: z.string().trim().min(8, 'validation.subject').max(160),
  description: z.string().trim().min(20, 'validation.description').max(10000),
});

export const complaintDraftSchema = identityStepSchema
  .merge(policyStepSchema)
  .merge(complaintStepSchema);

export type ComplaintDraftValues = z.infer<typeof complaintDraftSchema>;

export const profileSchema = z.object({
  full_name: z.string().trim().min(2, 'validation.fullName').max(120),
  email: z
    .string()
    .trim()
    .max(254)
    .refine((v) => v === '' || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v), {
      message: 'validation.email',
    }),
  whatsapp_available: z.boolean(),
  notify_email: z.boolean(),
  notify_whatsapp: z.boolean(),
  notify_sms: z.boolean(),
});

export type FieldErrorMap = Record<string, string>;

/** Flatten a ZodError into `{ field: firstMessageKey }` for live rendering. */
export function zodFieldErrors(error: z.ZodError): FieldErrorMap {
  const map: FieldErrorMap = {};
  for (const issue of error.issues) {
    const field = String(issue.path[0] ?? 'form');
    if (!(field in map)) map[field] = issue.message;
  }
  return map;
}
