/** Attachment rules mirrored from the backend upload plan (5 files / 25 MB / 10 MB each). */

export const MAX_FILES = 5;
export const MAX_FILE_BYTES = 10_485_760;
export const MAX_TOTAL_BYTES = 25_000_000;
export const ACCEPTED_MIME = ['application/pdf', 'image/png', 'image/jpeg'] as const;
export const ACCEPT_ATTR = '.pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg';

export type QueuedFileStatus = 'queued' | 'uploading' | 'done' | 'error';

export interface QueuedFile {
  id: string;
  file: File;
  status: QueuedFileStatus;
  progress: number;
  error?: string;
  previewUrl?: string;
}

let sequence = 0;

export function toQueuedFile(file: File): QueuedFile {
  sequence += 1;
  return {
    id: `${Date.now()}-${sequence}-${file.name}`,
    file,
    status: 'queued',
    progress: 0,
    previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
  };
}

export type FileValidationError = 'tooMany' | 'tooLarge' | 'totalTooLarge' | 'badType';

/** Validate files being added to an existing queue. Returns the first problem found. */
export function validateNewFiles(existing: File[], incoming: File[]): FileValidationError | null {
  if (existing.length + incoming.length > MAX_FILES) return 'tooMany';
  if (incoming.some((file) => !ACCEPTED_MIME.includes(file.type as (typeof ACCEPTED_MIME)[number]))) {
    return 'badType';
  }
  if (incoming.some((file) => file.size > MAX_FILE_BYTES)) return 'tooLarge';
  const total = [...existing, ...incoming].reduce((sum, file) => sum + file.size, 0);
  if (total > MAX_TOTAL_BYTES) return 'totalTooLarge';
  return null;
}
