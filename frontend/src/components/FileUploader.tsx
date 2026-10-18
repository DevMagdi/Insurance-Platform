import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText, Image as ImageIcon, Plus, RotateCcw, Trash2, UploadCloud } from 'lucide-react';
import { ACCEPT_ATTR, validateNewFiles, type FileValidationError, type QueuedFile } from '../lib/files';
import { formatBytes } from '../lib/status';
import { Button } from './ui';

const ERROR_KEYS: Record<FileValidationError, string> = {
  tooMany: 'files.tooMany',
  tooLarge: 'files.tooLarge',
  totalTooLarge: 'files.totalTooLarge',
  badType: 'files.badType',
  badName: 'files.badName',
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
    const problem = validateNewFiles(files.map((q) => q.file), list);
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
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            inputRef.current?.click();
          }
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
        className={`dropzone${dragging ? ' is-dragging' : ''}`}
      >
        <span className="dropzone-icon"><UploadCloud size={20} strokeWidth={1.6} /></span>
        <strong>{t('files.drop')}</strong>
        <span>{t('files.limits')}</span>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPT_ATTR}
          className="sr-only"
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            if (e.target.files) handleFiles(e.target.files);
            e.target.value = '';
          }}
        />
      </div>

      {error && (
        <p role="alert" className="field-error" style={{ marginTop: 6 }}>
          {t(ERROR_KEYS[error])}
        </p>
      )}

      {files.length > 0 && (
        <ul className="file-list">
          {files.map((queued) => (
            <li key={queued.id} className="file-row">
              <span className="file-thumb">
                {queued.previewUrl ? (
                  <img src={queued.previewUrl} alt="" />
                ) : queued.file.type === 'application/pdf' ? (
                  <FileText size={16} />
                ) : (
                  <ImageIcon size={16} />
                )}
              </span>
              <div className="file-body">
                <span className="file-name">{queued.file.name}</span>
                <span className="file-meta">
                  {formatBytes(queued.file.size)}
                  {queued.status === 'done' && <span className="ok">· ✓ {t('files.pending')}</span>}
                  {queued.status === 'queued' && <span>· {t('common.savedDraft')}</span>}
                  {queued.status === 'error' && <span className="err" role="alert">· {queued.error ?? t('errors.generic')}</span>}
                </span>
                {queued.status === 'uploading' && (
                  <div className="progress" role="progressbar" aria-valuenow={queued.progress} aria-valuemin={0} aria-valuemax={100}>
                    <div className="progress-fill" style={{ width: `${queued.progress}%` }} />
                  </div>
                )}
              </div>
              {queued.status === 'error' && onRetry && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="retry"
                  onClick={() => onRetry(queued.id)}
                  leadingIcon={<RotateCcw size={12} />}
                >
                  {t('common.retry')}
                </Button>
              )}
              {queued.status !== 'uploading' && (
                <button
                  type="button"
                  className="file-action"
                  aria-label={`${t('common.remove')} ${queued.file.name}`}
                  onClick={() => onRemove(queued.id)}
                  title={t('common.remove')}
                >
                  <Trash2 size={14} />
                  <span className="sr-only">{t('common.remove')}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {files.length === 0 && (
        <p className="field-hint" style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 5 }}>
          <Plus size={12} />
          {t('files.limits')}
        </p>
      )}
    </div>
  );
}
