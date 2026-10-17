import type { ReactNode } from 'react';
import { cn } from '../../lib/utils';

export function Card({
  children,
  className,
  variant = 'solid',
  ...props
}: {
  children: ReactNode;
  className?: string;
  variant?: 'solid' | 'glass' | 'flat' | 'inset';
  id?: string;
}) {
  const variants = {
    solid: 'card',
    glass: 'card card-glass',
    flat: 'card card-flat',
    inset: 'card card-inset',
  };
  return (
    <div className={cn(variants[variant], className)} {...props}>
      {children}
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
  action,
  align = 'start',
  className,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  align?: 'start' | 'center';
  className?: string;
}) {
  return (
    <div className={cn('section-head', align === 'center' && 'center', className)}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
        <div style={{ maxWidth: 640 }}>
          {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
          <h2>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        {action}
      </div>
    </div>
  );
}
