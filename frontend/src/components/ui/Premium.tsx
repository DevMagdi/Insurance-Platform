/**
 * Premium design-system primitives.
 *
 * These are the pieces every redesigned screen is assembled from. They own no
 * colour of their own — each reads the tokens in `design-system.css`, so the
 * light and dark renderings stay in agreement and a palette change is a
 * one-file change.
 *
 * Motion is deliberately restrained: 140–320ms, small distances, and every
 * animation yields to `prefers-reduced-motion`.
 */
import {
  forwardRef,
  type CSSProperties,
  type HTMLAttributes,
  type ReactNode,
} from 'react';
import { motion, useReducedMotion, type HTMLMotionProps } from 'framer-motion';
import { cn } from '../../lib/utils';

/* ---------------------------------------------------------------- motion */

/** The house easing curve, matching `--ease-out` in CSS. */
const EASE = [0.22, 0.61, 0.36, 1] as const;

/**
 * Staggered entrance for a list or grid. Children rise 10px over 320ms, 45ms
 * apart — enough to read as deliberate, short enough never to delay a click.
 */
export function Stagger({
  children,
  className,
  delay = 0,
  gap = 0.045,
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
  gap?: number;
}) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      initial="hidden"
      animate="shown"
      variants={{ shown: { transition: { staggerChildren: gap, delayChildren: delay } } }}
    >
      {children}
    </motion.div>
  );
}

/**
 * A panel that crossfades when its `routeKey` changes — tab bodies, wizard
 * steps, anything that swaps in place. Without it a tab switch is a hard cut
 * and the eye has to re-acquire the whole region; 180ms of fade plus 6px of
 * rise keeps the thread.
 */
export function SwapPanel({
  swapKey,
  children,
  className,
}: {
  swapKey: string;
  children: ReactNode;
  className?: string;
}) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      key={swapKey}
      className={className}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: EASE }}
    >
      {children}
    </motion.div>
  );
}

export function StaggerItem({
  children,
  className,
  ...rest
}: { children: ReactNode; className?: string } & HTMLMotionProps<'div'>) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      variants={{
        hidden: { opacity: 0, y: 10 },
        shown: { opacity: 1, y: 0, transition: { duration: 0.32, ease: EASE } },
      }}
      {...rest}
    >
      {children}
    </motion.div>
  );
}

/* ------------------------------------------------------------------ card */

export interface PanelProps extends HTMLAttributes<HTMLDivElement> {
  /** Adds hover lift and a focus ring; use when the whole card is a target. */
  interactive?: boolean;
  /** Removes the default padding so the card can hold a flush table. */
  flush?: boolean;
  /** A soft radial tint in the far corner. Pass any CSS colour. */
  wash?: string;
  children?: ReactNode;
}

/**
 * The surface everything sits on: one hairline border, one soft shadow, and
 * in dark mode a 2.5% sheen down the first 120px so it reads as a lit plane.
 */
export const Panel = forwardRef<HTMLDivElement, PanelProps>(function Panel(
  { interactive, flush, wash, className, style, children, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'ds-card',
        !flush && 'ds-card-pad',
        interactive && 'ds-card-interactive ds-focusable',
        wash && 'ds-wash',
        className,
      )}
      style={wash ? ({ ...style, '--wash': wash } as CSSProperties) : style}
      {...props}
    >
      {children}
    </div>
  );
});

/** Title + optional subtitle on the start side, actions on the end side. */
export function PanelHeader({
  title,
  subtitle,
  eyebrow,
  actions,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('ds-section-head', className)}>
      <div className="min-w-0">
        {eyebrow && <span className="ds-eyebrow">{eyebrow}</span>}
        <h2 className="ds-section-title">{title}</h2>
        {subtitle && <p className="ds-section-sub">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------ status pill */

export type PillTone =
  | 'new'
  | 'assigned'
  | 'in_review'
  | 'resolved'
  | 'reopened'
  | 'transferred'
  | 'neutral'
  | 'gold';

/**
 * The canonical status pill. Colour comes from `--st-*`, so a pill, a legend
 * swatch and a chart series cannot drift apart.
 */
export function StatusPill({
  tone,
  children,
  className,
}: {
  tone: PillTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={cn('ds-pill', className)} data-tone={tone}>
      {children}
    </span>
  );
}

/** Metadata, not state: no dot, quieter fill. */
export function Tag({
  children,
  className,
  icon,
}: {
  children: ReactNode;
  className?: string;
  icon?: ReactNode;
}) {
  return (
    <span className={cn('ds-tag', className)}>
      {icon}
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ metric */

/**
 * A KPI. The number is the loudest thing in the card; the label sits above it
 * small and quiet, and any delta sits below so the eye lands on the value
 * first and the context second.
 */
export function Metric({
  label,
  value,
  hint,
  icon,
  tone = 'gold',
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  tone?: 'gold' | 'info' | 'success' | 'warning' | 'danger' | 'violet';
  className?: string;
}) {
  /* Tone is declared, never painted here. The alphas that read as "a hint of
     hue" on near-black read as pastel confetti on white, so the two sets live
     in `design-system.css` behind `--wash-*` and this component only says
     which one it wants. */
  return (
    <Panel data-tone={tone} className={cn('ds-metric-card ds-wash', className)}>
      <div className="ds-metric-top">
        <span className="ds-metric-label">{label}</span>
        {icon && (
          <span className="ds-metric-icon" aria-hidden="true">
            {icon}
          </span>
        )}
      </div>
      <div className="ds-metric ds-metric-value">{value}</div>
      {hint && <div className="ds-metric-hint">{hint}</div>}
    </Panel>
  );
}

/* ------------------------------------------------------------------ empty */

/** A calm empty state: an icon in a soft disc, a line, and one way forward. */
export function Blank({
  icon,
  title,
  body,
  action,
  className,
}: {
  icon?: ReactNode;
  title: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('ds-blank', className)}>
      {icon && <span className="ds-blank-icon" aria-hidden="true">{icon}</span>}
      <strong className="ds-blank-title">{title}</strong>
      {body && <p className="ds-blank-body">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** Placeholder lines while data loads, sized like the content they replace. */
export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-3', className)} aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="ds-skeleton" style={{ height: 52, opacity: 1 - i * 0.12 }} />
      ))}
    </div>
  );
}
