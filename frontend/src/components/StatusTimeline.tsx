import { motion } from 'framer-motion';
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
  const current = timelineStepIndex(status);
  const transferred = status === 'transferred';
  const progress = transferred ? 66 : (current / (MAIN_STEPS.length - 1)) * 100;

  return (
    <section aria-label={t('timeline.title')} className="rounded-3xl border border-white/10 bg-white/60 p-5 shadow-sm backdrop-blur dark:bg-white/5">
      <div className="mb-5 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{t('timeline.title')}</h2>
        <StatusChip status={status} />
      </div>

      <div className="relative mx-2" dir="ltr">
        <div className="absolute inset-x-0 top-4 h-1 rounded-full bg-slate-200 dark:bg-white/10" />
        <motion.div
          className="absolute top-4 h-1 rounded-full bg-gradient-to-r from-amber-400 to-emerald-400"
          initial={{ width: 0 }}
          animate={{ width: `${progress}%` }}
          transition={{ duration: 0.9, ease: 'easeOut' }}
          style={{ insetInlineStart: 0 }}
        />
        <ol className="relative flex justify-between">
          {MAIN_STEPS.map((step, index) => {
            const reached = index <= current || (transferred && index <= 2);
            const isCurrent = index === current && !transferred;
            return (
              <li key={step} className="flex w-16 flex-col items-center gap-2 text-center">
                <motion.span
                  initial={{ scale: 0.6, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  transition={{ delay: index * 0.15 }}
                  className={`flex h-8 w-8 items-center justify-center rounded-full ring-4 ring-white text-xs font-bold dark:ring-slate-900 ${
                    reached
                      ? 'bg-gradient-to-br from-amber-300 to-emerald-400 text-slate-950'
                      : 'bg-slate-200 text-slate-500 dark:bg-white/10 dark:text-slate-400'
                  } ${isCurrent ? 'animate-pulse' : ''}`}
                >
                  {index + 1}
                </motion.span>
                <span className={`text-[11px] leading-tight sm:text-xs ${reached ? 'font-semibold' : 'text-slate-400'}`}>
                  {t(STATUS_META[step].labelKey)}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      {transferred && (
        <motion.p
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          className="mt-4 rounded-xl bg-fuchsia-500/10 px-3 py-2 text-sm text-fuchsia-600 dark:text-fuchsia-300"
        >
          {t('status.transferred')}
        </motion.p>
      )}
      {status === 'reopened' && (
        <p className="mt-4 rounded-xl bg-orange-500/10 px-3 py-2 text-sm text-orange-600 dark:text-orange-300">
          {t('status.reopened')}
        </p>
      )}

      {events.length > 0 && (
        <div className="mt-6 border-t border-slate-200/70 pt-4 dark:border-white/10">
          <h3 className="mb-3 text-sm font-semibold text-slate-500 dark:text-slate-300">{t('timeline.events')}</h3>
          <ol className="space-y-2.5">
            {[...events].reverse().map((event) => (
              <motion.li
                key={event.id}
                initial={{ opacity: 0, x: 8 }}
                animate={{ opacity: 1, x: 0 }}
                className="flex items-center justify-between gap-3 text-sm"
              >
                <span className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full ${event.to_status ? STATUS_META[event.to_status].dotClass : 'bg-slate-400'}`} />
                  {t(eventLabelKey(event))}
                </span>
                <time className="text-xs text-slate-400">{formatDate(event.occurred_at, i18n.language)}</time>
              </motion.li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
