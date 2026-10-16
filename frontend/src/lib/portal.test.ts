import { describe, expect, it } from 'vitest';
import { clearDraft, emptyDraft, loadDraft, loadDraftFileNames, saveDraft, saveDraftFileNames } from './draft';
import { MAX_FILE_BYTES, validateNewFiles } from './files';
import { MAIN_STEPS, isOpenStatus, timelineStepIndex } from './status';

describe('complaint draft autosave', () => {
  it('round-trips a draft and survives clearing', () => {
    const draft = { ...emptyDraft(), full_name: 'Mona Ali', subject: 'Delayed claim', step: 2 };
    saveDraft(draft);
    const loaded = loadDraft();
    expect(loaded?.full_name).toBe('Mona Ali');
    expect(loaded?.idempotency_key).toBe(draft.idempotency_key);
    saveDraftFileNames(['proof.pdf', 'photo.png']);
    expect(loadDraftFileNames()).toEqual(['proof.pdf', 'photo.png']);
    clearDraft();
    expect(loadDraft()).toBeNull();
    expect(loadDraftFileNames()).toEqual([]);
  });

  it('ignores corrupted storage', () => {
    localStorage.setItem('insurance-portal:complaint-draft:v1', '{broken');
    expect(loadDraft()).toBeNull();
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
