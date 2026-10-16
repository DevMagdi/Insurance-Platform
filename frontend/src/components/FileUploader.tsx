import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ACCEPT_ATTR, validateNewFiles, type FileValidationError, type QueuedFile } from '../lib/files';
import { formatBytes } from '../lib/status';
import { ghostButton } from './ui';

const ERROR_KEYS: Record<FileValidationError, string> = {
  tooMany: 'files.tooMany',
  tooLarge: 'files.tooLarge',
  totalTooLarge: 'files.totalTooLarge',
  badType: 'files.badType',
};

/**
 * Drag & drop attachment picker with image previews, per-file progress,
 * remove and retry. Upload itself is owned by the parent (wizard defers it
 * until the complaint exists; the ticket page uploads immediately).
 */
export function FileUploader({
  files,
  onAdd,
  onRemove,
  onRetry,
}: {
  files: QueuedFile[];
  onAdd: (files: File[]) => void;
  onRemove: (id: string) => void;
  onRetry?: (id: string) => void;
}) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<FileValidationError | null>(null);

  const handleFiles = (incoming: FileList | File[]) => {
    const list = [...incoming];
    if (list.length === 0) return;
    const problem = validateNewFiles(
      files.map((q) => q.file),
      list,
    );
    setError(problem);
    if (!problem) onAdd(list);
  };

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        aria-label={t('files.drop')}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click();
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer.files);
        }}
        className={`cursor-pointer rounded-2xl border-2 border-dashed p-6 text-center transition ${
          dragging
            ? 'border-amber-400 bg-amber-400/10'
            : 'border-slate-300/70 hover:border-amber-300 dark:border-white/15'
        }`}
      >
        <p className="font-medium">{t('files.drop')}</p>
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{t('files.limits')}</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPT_ATTR}
          className="hidden"
          aria-hidden="true"
          onChange={(e) => {
            if (e.target.files) handleFiles(e.target.files);
            e.target.value = '';
          }}
        />
      </div>

      {error && (
        <p role="alert" className="mt-2 text-sm text-rose-500">
          {t(ERROR_KEYS[error])}
        </p>
      )}

      {files.length > 0 && (
        <ul className="mt-4 space-y-3">
          {files.map((queued) => (
            <li
              key={queued.id}
              className="flex items-center gap-3 rounded-2xl border border-slate-200/80 bg-white/60 p-3 dark:border-white/10 dark:bg-white/5"
            >
              {queued.previewUrl ? (
                <img src={queued.previewUrl} alt="" className="h-12 w-12 rounded-lg object-cover" />
              ) : (
                <span className="flex h-12 w-12 items-center justify-center rounded-lg bg-rose-500/10 text-xs font-bold text-rose-500">
                  PDF
                </span>
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{queued.file.name}</p>
                <p className="text-xs text-slate-500">{formatBytes(queued.file.size)}</p>
                {queued.status === 'uploading' && (
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-white/10" role="progressbar" aria-valuenow={queued.progress}>
                    <div className="h-full bg-gradient-to-l from-amber-400 to-emerald-400 transition-all" style={{ width: `${queued.progress}%` }} />
                  </div>
                )}
                {queued.status === 'done' && <p className="mt-0.5 text-xs text-emerald-500">✓ {t('files.pending')}</p>}
                {queued.status === 'error' && (
                  <p className="mt-0.5 text-xs text-rose-500" role="alert">
                    {queued.error ?? t('errors.generic')}
                  </p>
                )}
              </div>
              {queued.status === 'error' && onRetry && (
                <button type="button" className={ghostButton} onClick={() => onRetry(queued.id)}>
                  {t('common.retry')}
                </button>
              )}
              {queued.status !== 'uploading' && (
                <button
                  type="button"
                  className={ghostButton}
                  aria-label={`${t('common.remove')} ${queued.file.name}`}
                  onClick={() => onRemove(queued.id)}
                >
                  {t('common.remove')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
