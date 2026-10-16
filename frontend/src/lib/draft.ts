/**
 * Autosaved complaint draft (localStorage, versioned).
 *
 * Only form text is persisted — file binaries cannot survive a reload, so the
 * wizard stores selected file *names* separately and asks the customer to
 * re-attach after a restore. The national ID is kept solely on this device so
 * an interrupted application can resume; it is cleared on successful submit.
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

export function loadDraft(): ComplaintDraft | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ComplaintDraft>;
    if (parsed.version !== 1 || typeof parsed.idempotency_key !== 'string') return null;
    return { ...emptyDraft(), ...parsed, version: 1 };
  } catch {
    return null;
  }
}

export function saveDraft(draft: ComplaintDraft): void {
  try {
    localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ ...draft, saved_at: new Date().toISOString() }),
    );
  } catch {
    // Storage full/blocked: autosave is best-effort, the form still works.
  }
}

export function clearDraft(): void {
  try {
    localStorage.removeItem(DRAFT_KEY);
    localStorage.removeItem(FILE_NAMES_KEY);
  } catch {
    // Ignore unavailable storage.
  }
}

export function saveDraftFileNames(names: string[]): void {
  try {
    localStorage.setItem(FILE_NAMES_KEY, JSON.stringify(names.slice(0, 5)));
  } catch {
    // Best-effort only.
  }
}

export function loadDraftFileNames(): string[] {
  try {
    const raw = localStorage.getItem(FILE_NAMES_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((n): n is string => typeof n === 'string') : [];
  } catch {
    return [];
  }
}
