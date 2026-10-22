import { useEffect, useId, useRef, type ReactNode } from 'react';
import { motion, useReducedMotion } from 'framer-motion';
import { Icon } from './Icon';

interface ModalProps {
  title: string;
  eyebrow?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  className?: string;
}

export function Modal({ title, eyebrow, children, onClose, wide = false, className = '' }: ModalProps) {
  const backdrop = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const reduced = useReducedMotion();
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('.modal-backdrop');
      if (event.key === 'Escape' && dialogs.item(dialogs.length - 1) === backdrop.current) onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    /* The dialog used to appear instantly, which reads as a page swap rather
       than a layer opening. 160ms of backdrop fade under a 200ms scale-from-
       98% is the whole trick: short enough that nobody waits for it, long
       enough that the eye follows the layer instead of re-finding it. */
    <motion.div
      ref={backdrop}
      className="modal-backdrop"
      role="presentation"
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.16, ease: [0.22, 0.61, 0.36, 1] }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.section
        aria-modal="true"
        className={`modal-card${wide ? ' modal-card-wide' : ''}${className ? ` ${className}` : ''}`}
        role="dialog"
        aria-labelledby={titleId}
        initial={reduced ? false : { opacity: 0, scale: 0.98, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.2, ease: [0.22, 0.61, 0.36, 1] }}
      >
        <div className="modal-heading">
          <div>
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            <h2 id={titleId}>{title}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="إغلاق">
            <Icon name="close" />
          </button>
        </div>
        <div className="modal-content">{children}</div>
      </motion.section>
    </motion.div>
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
