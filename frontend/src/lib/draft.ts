/**
 * Minimal complaint wizard resume metadata. Form values are deliberately kept
 * in memory only: localStorage is readable by every script running on the same
 * origin and must not contain customer data, free text, or file names.
 */
import type { InsuranceType } from './types';

export interface ComplaintDraft {
  version: 1;
  step: number;
  full_name: string;
  national_id: string;
  mobile: string;
  whatsapp_available: boolean;
  email: string;
  company_id: string;
  insurance_type: InsuranceType | '';
  policy_number: string;
  subject: string;
  description: string;
  /** Stable key so a retried submission replays instead of duplicating. */
  idempotency_key: string;
  saved_at: string;
}

const DRAFT_KEY = 'insurance-portal:complaint-draft:v1';
const FILE_NAMES_KEY = 'insurance-portal:complaint-draft-files:v1';

export const emptyDraft = (): ComplaintDraft => ({
  version: 1,
  step: 0,
  full_name: '',
  national_id: '',
  mobile: '',
  whatsapp_available: false,
  email: '',
  company_id: '',
  insurance_type: '',
  policy_number: '',
  subject: '',
  description: '',
  idempotency_key:
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `draft-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  saved_at: new Date().toISOString(),
});

function scrubStoredDraft(step: number, idempotencyKey: string, savedAt?: string): void {
  try {
    // Never serialize user-supplied form fields, even if the object gains new
    // fields in the future. This also migrates old v1 drafts without preserving PII.
    localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        version: 1,
        step: Math.min(4, Math.max(0, step)),
        idempotency_key: idempotencyKey,
        saved_at: savedAt ?? new Date().toISOString(),
      }),
    );
    localStorage.removeItem(FILE_NAMES_KEY);
  } catch {
    // Storage full/blocked: resume metadata is best-effort.
  }
}

export function loadDraft(): ComplaintDraft | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ComplaintDraft>;
    if (parsed.version !== 1 || typeof parsed.idempotency_key !== 'string') {
      clearDraft();
      return null;
    }

    // Treat every previously stored draft as untrusted legacy data. Migrate it
    // immediately and return blank form values; do not expose old PII to React.
    const draft = {
      ...emptyDraft(),
      step: typeof parsed.step === 'number' ? parsed.step : 0,
      idempotency_key: parsed.idempotency_key,
      saved_at: typeof parsed.saved_at === 'string' ? parsed.saved_at : new Date().toISOString(),
    };
    scrubStoredDraft(draft.step, draft.idempotency_key, draft.saved_at);
    return draft;
  } catch {
    clearDraft();
    return null;
  }
}

export function saveDraft(draft: ComplaintDraft): void {
  scrubStoredDraft(draft.step, draft.idempotency_key);
}

export function clearDraft(): void {
  try {
    localStorage.removeItem(DRAFT_KEY);
    localStorage.removeItem(FILE_NAMES_KEY);
  } catch {
    // Ignore unavailable storage.
  }
}

/** File names may themselves contain personal data, so they are never persisted. */
export function saveDraftFileNames(names: string[]): void {
  // Intentionally discard filenames because they may contain personal data.
  void names;
  try {
    localStorage.removeItem(FILE_NAMES_KEY);
  } catch {
    // Best-effort only.
  }
}

export function loadDraftFileNames(): string[] {
  try {
    localStorage.removeItem(FILE_NAMES_KEY);
  } catch {
    // Best-effort only.
  }
  return [];
}
