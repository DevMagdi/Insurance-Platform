import { describe, expect, it } from 'vitest';
import { clearDraft, emptyDraft, loadDraft, loadDraftFileNames, saveDraft, saveDraftFileNames } from './draft';
import { MAX_FILE_BYTES, validateNewFiles } from './files';
import { MAIN_STEPS, isOpenStatus, timelineStepIndex } from './status';

describe('complaint draft autosave', () => {
  it('persists resume metadata only and clears legacy PII and file names', () => {
    const draft = {
      ...emptyDraft(),
      full_name: 'Mona Ali',
      national_id: '29901010112345',
      mobile: '+201123456789',
      subject: 'Delayed claim',
      description: 'Private complaint details',
      step: 2,
    };
    saveDraft(draft);
    saveDraftFileNames(['mona-national-id.pdf']);
    const raw = localStorage.getItem('insurance-portal:complaint-draft:v1') ?? '';
    expect(raw).not.toContain('Mona Ali');
    expect(raw).not.toContain('29901010112345');
    expect(raw).not.toContain('+201123456789');
    expect(raw).not.toContain('Delayed claim');
    expect(raw).not.toContain('Private complaint details');
    expect(raw).not.toContain('mona-national-id.pdf');

    const loaded = loadDraft();
    expect(loaded?.step).toBe(2);
    expect(loaded?.national_id).toBe('');
    expect(loaded?.full_name).toBe('');
    expect(loaded?.idempotency_key).toBe(draft.idempotency_key);
    expect(loadDraftFileNames()).toEqual([]);
    clearDraft();
    expect(loadDraft()).toBeNull();
  });

  it('migrates legacy drafts without restoring personal data', () => {
    localStorage.setItem(
      'insurance-portal:complaint-draft:v1',
      JSON.stringify({ ...emptyDraft(), full_name: 'Mona Ali', national_id: '29901010112345' }),
    );
    expect(loadDraft()?.full_name).toBe('');
    expect(localStorage.getItem('insurance-portal:complaint-draft:v1')).not.toContain('29901010112345');
  });

  it('ignores corrupted storage', () => {
    localStorage.setItem('insurance-portal:complaint-draft:v1', '{broken');
    expect(loadDraft()).toBeNull();
    expect(localStorage.getItem('insurance-portal:complaint-draft:v1')).toBeNull();
  });
});

describe('attachment limits', () => {
  const file = (name: string, size: number, type = 'application/pdf') =>
    new File([new Uint8Array(size)], name, { type });

  it('enforces count, type, per-file and total limits', () => {
    expect(validateNewFiles([], [file('a.pdf', 100)])).toBeNull();
    expect(validateNewFiles([file('a', 1), file('b', 1), file('c', 1), file('d', 1), file('e', 1)], [file('f.pdf', 1)])).toBe('tooMany');
    expect(validateNewFiles([], [file('a.txt', 1, 'text/plain')])).toBe('badType');
    expect(validateNewFiles([], [file('big.pdf', MAX_FILE_BYTES + 1)])).toBe('tooLarge');
    expect(
      validateNewFiles([file('a.pdf', 9_000_000), file('b.pdf', 9_000_000)], [file('c.pdf', 9_000_000)]),
    ).toBe('totalTooLarge');
  });
});

describe('status timeline mapping', () => {
  it('maps every status onto the four-step journey', () => {
    expect(MAIN_STEPS).toEqual(['new', 'assigned', 'in_review', 'resolved']);
    expect(timelineStepIndex('new')).toBe(0);
    expect(timelineStepIndex('assigned')).toBe(1);
    expect(timelineStepIndex('in_review')).toBe(2);
    expect(timelineStepIndex('reopened')).toBe(2); // loops back to In Review
    expect(timelineStepIndex('resolved')).toBe(3);
    expect(isOpenStatus('transferred')).toBe(true);
    expect(isOpenStatus('resolved')).toBe(false);
  });
});
