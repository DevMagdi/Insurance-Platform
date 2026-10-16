import { motion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import { navigate } from '../lib/router';
import { ghostButton, primaryButton } from '../components/ui';
import { TrackForm } from './TrackPage';

export function HomePage() {
  const { t } = useTranslation();
  return (
    <div className="space-y-10">
      <section className="overflow-hidden rounded-[2rem] border border-white/20 bg-gradient-to-b from-[#0b2440] to-[#081a2f] p-6 text-white shadow-2xl sm:p-10">
        <motion.h1
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          className="max-w-2xl text-3xl font-bold leading-snug sm:text-4xl"
        >
          {t('hero.title')}
        </motion.h1>
        <p className="mt-4 max-w-2xl leading-8 text-slate-300">{t('hero.subtitle')}</p>
        <div className="mt-7 flex flex-wrap gap-3">
          <button type="button" className={primaryButton} onClick={() => navigate('/complaints/new')}>
            {t('hero.ctaFile')}
          </button>
          <button
            type="button"
            className="inline-flex items-center justify-center rounded-xl border border-white/25 px-5 py-2.5 font-semibold text-white transition hover:bg-white/10"
            onClick={() => navigate('/track')}
          >
            {t('hero.ctaTrack')}
          </button>
        </div>
        <ul className="mt-8 grid gap-3 text-sm text-slate-200 sm:grid-cols-3">
          {[t('hero.point1'), t('hero.point2'), t('hero.point3')].map((point) => (
            <li key={point} className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 backdrop-blur">
              ✦ {point}
            </li>
          ))}
        </ul>
      </section>

      <section className="mx-auto max-w-2xl rounded-3xl border border-slate-200/80 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="mb-4 text-lg font-semibold">{t('track.title')}</h2>
        <TrackForm />
      </section>

      <div className="text-center">
        <button type="button" className={ghostButton} onClick={() => navigate('/tickets')}>
          {t('nav.tickets')}
        </button>
      </div>
    </div>
  );
}
