import { useEffect, type ReactNode } from 'react';
import { Icon } from './Icon';

interface ModalProps {
  title: string;
  eyebrow?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}

export function Modal({ title, eyebrow, children, onClose, wide = false }: ModalProps) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        aria-modal="true"
        className={`modal-card${wide ? ' modal-card-wide' : ''}`}
        role="dialog"
        aria-labelledby="modal-title"
      >
        <div className="modal-heading">
          <div>
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            <h2 id="modal-title">{title}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="إغلاق">
            <Icon name="close" />
          </button>
        </div>
        <div className="modal-content">{children}</div>
      </section>
    </div>
  );
}

interface SectionTitleProps {
  title: string;
  description?: string;
  action?: ReactNode;
  eyebrow?: string;
}

export function SectionTitle({ title, description, action, eyebrow }: SectionTitleProps) {
  return (
    <div className="section-title-row">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h2>{title}</h2>
        {description && <p className="section-description">{description}</p>}
      </div>
      {action && <div className="section-title-action">{action}</div>}
    </div>
  );
}
