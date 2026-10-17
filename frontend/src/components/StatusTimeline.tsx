import { motion, useReducedMotion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import type { ComplaintStatus, TimelineEvent } from '../lib/types';
import { MAIN_STEPS, STATUS_META, eventLabelKey, formatDate, timelineStepIndex } from '../lib/status';
import { StatusChip } from './ui';

/**
 * Animated journey: New → Assigned → In Review → Resolved, with Reopened
 * looping back to In Review and Transferred shown as a terminal branch.
 */
export function StatusTimeline({
  status,
  events,
}: {
  status: ComplaintStatus;
  events: TimelineEvent[];
}) {
  const { t, i18n } = useTranslation();
  const reduced = useReducedMotion();
  const current = timelineStepIndex(status);
  const transferred = status === 'transferred';
  const progress = transferred ? 66 : (current / (MAIN_STEPS.length - 1)) * 100;

  return (
    <section aria-label={t('timeline.title')} className="card journey-card">
      <div className="journey-head">
        <h2>{t('timeline.title')}</h2>
        <StatusChip status={status} />
      </div>

      <div className="journey-rail" style={{ ['--journey-progress' as string]: `${progress}%` }}>
        {MAIN_STEPS.map((step, index) => {
          const reached = index <= current || (transferred && index <= 2);
          const isCurrent = index === current && !transferred;
          return (
            <div key={step} className={`journey-step${reached ? ' reached' : ''}${isCurrent ? ' current' : ''}`}>
              <motion.span
                className="journey-dot"
                initial={reduced ? undefined : { scale: 0.7, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ delay: index * 0.07, duration: 0.22 }}
                aria-current={isCurrent ? 'step' : undefined}
              >
                {reached ? '✓' : index + 1}
              </motion.span>
              <span className="journey-label">{t(STATUS_META[step].labelKey)}</span>
            </div>
          );
        })}
      </div>

      {transferred && <p className="journey-note transferred">{t('status.transferred')}</p>}
      {status === 'reopened' && <p className="journey-note reopened">{t('status.reopened')}</p>}

      {events.length > 0 && (
        <div className="journey-events">
          <h3 className="eyebrow" style={{ marginBottom: 2 }}>{t('timeline.events')}</h3>
          <ol style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {[...events].reverse().map((event) => (
              <li key={event.id} className="journey-event">
                <span>
                  <i aria-hidden="true" />
                  {t(eventLabelKey(event))}
                </span>
                <time>{formatDate(event.occurred_at, i18n.language)}</time>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
