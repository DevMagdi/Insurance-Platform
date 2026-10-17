import { motion, useReducedMotion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  ArrowRight,
  BadgeCheck,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileCheck2,
  FilePlus2,
  LockKeyhole,
  MessageSquareText,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  UserRound,
} from 'lucide-react';
import { navigate } from '../lib/router';
import { Button } from '../components/ui';
import { StatusChip } from '../components/ui/Badge';
import { TrackForm } from './TrackPage';

function HeroCard() {
  const { t, i18n } = useTranslation();
  const Arrow = i18n.language === 'ar' ? ArrowLeft : ArrowRight;
  const steps = [
    { label: t('home.mockStep1'), time: '09:12', state: 'done' as const, icon: Check },
    { label: t('home.mockStep2'), time: '09:40', state: 'done' as const, icon: UserRound },
    { label: t('home.mockStep3'), time: t('home.mockUpdated'), state: 'current' as const, icon: MessageSquareText },
    { label: t('home.mockStep4'), time: '—', state: 'pending' as const, icon: ShieldCheck },
  ];

  return (
    <div className="hero-visual" aria-hidden="true">
      <div className="hero-card">
        <div className="hero-card-head">
          <span className="chip chip-gold">
            <Sparkles size={11} />
            {t('home.previewTag')}
          </span>
          <span className="hero-card-id" dir="ltr">{t('home.mockTicket')}</span>
        </div>
        <p className="hero-card-title">{t('home.mockTitle')}</p>
        <div className="hero-card-meta">
          <span>{t('home.mockCompany')}</span>
          <span className="dot" />
          <StatusChip status="in_review" />
        </div>
        <div className="hero-divider" />
        <div className="hero-timeline">
          {steps.map((step) => {
            const Icon = step.icon;
            return (
              <div key={step.label} className={`hero-timeline-row${step.state === 'current' ? ' is-current' : ''}`}>
                <span className={`hero-timeline-dot ${step.state}`}>
                  <Icon size={11} />
                </span>
                <span className="hero-timeline-label">{step.label}</span>
                <span className="hero-timeline-time">{step.time}</span>
              </div>
            );
          })}
        </div>
        <div className="hero-card-footer">
          <Clock3 size={12} />
          <span>{t('home.mockUpdated')}</span>
          <span style={{ marginInlineStart: 'auto' }} className="chip chip-emerald">
            <span className="dot dot-live" />
            {t('home.mockSlaValue')}
          </span>
        </div>
      </div>

      <div className="hero-float hero-float-1">
        <span className="hero-float-icon" style={{ background: 'var(--emerald-100)', color: 'var(--emerald-700)' }}>
          <FileCheck2 size={15} />
        </span>
        <span>
          <strong>{t('home.float1')}</strong>
          <span>{t('home.float1sub')}</span>
        </span>
      </div>

      <div className="hero-float hero-float-2">
        <span className="hero-float-icon" style={{ background: 'var(--gold-100)', color: 'var(--gold-700)' }}>
          <BadgeCheck size={15} />
        </span>
        <span>
          <strong>{t('home.float2')}</strong>
          <span>{t('home.float2sub')}</span>
        </span>
        <Arrow size={13} style={{ color: 'var(--ink-300)' }} />
      </div>
    </div>
  );
}

export function HomePage() {
  const { t, i18n } = useTranslation();
  const reduced = useReducedMotion();
  const Arrow = i18n.language === 'ar' ? ArrowLeft : ArrowRight;
  const fade = reduced
    ? {}
    : { initial: { opacity: 0, y: 14 }, animate: { opacity: 1, y: 0 } };

  const steps = [
    {
      icon: FilePlus2,
      title: t('home.step1Title'),
      body: t('home.step1Body'),
      meta: t('home.step1Meta'),
    },
    {
      icon: UserRound,
      title: t('home.step2Title'),
      body: t('home.step2Body'),
      meta: t('home.step2Meta'),
    },
    {
      icon: CheckCircle2,
      title: t('home.step3Title'),
      body: t('home.step3Body'),
      meta: t('home.step3Meta'),
    },
  ];

  const security = [
    { icon: LockKeyhole, title: t('home.security1Title'), body: t('home.security1Body') },
    { icon: ShieldCheck, title: t('home.security2Title'), body: t('home.security2Body') },
    { icon: BadgeCheck, title: t('home.security3Title'), body: t('home.security3Body') },
  ];

  const faqs = [
    [t('home.faq1q'), t('home.faq1a')],
    [t('home.faq2q'), t('home.faq2a')],
    [t('home.faq3q'), t('home.faq3a')],
    [t('home.faq4q'), t('home.faq4a')],
  ];

  return (
    <div className="home">
      <section className="hero" id="top">
        <div className="hero-grid">
          <motion.div className="hero-copy" {...fade} transition={{ duration: 0.5, ease: [0.22, 0.61, 0.36, 1] }}>
            <span className="hero-eyebrow">
              <span className="tag">
                <span className="dot dot-live" />
                {t('home.badgeTag')}
              </span>
              {t('home.badge')}
            </span>
            <h1>{t('hero.title')}</h1>
            <p className="hero-lead">{t('hero.subtitle')}</p>
            <div className="hero-actions">
              <Button size="lg" onClick={() => navigate('/complaints/new')} trailingIcon={<Arrow size={16} />}>
                {t('hero.ctaFile')}
              </Button>
              <Button size="lg" variant="secondary" onClick={() => navigate('/track')} leadingIcon={<Search size={15} />}>
                {t('hero.ctaTrack')}
              </Button>
            </div>
            <ul className="hero-points">
              {[t('hero.point1'), t('hero.point2'), t('hero.point3')].map((point) => (
                <li key={point}>
                  <Check size={13} strokeWidth={2.4} />
                  {point}
                </li>
              ))}
            </ul>
          </motion.div>

          <motion.div {...fade} transition={{ duration: 0.55, delay: 0.08, ease: [0.22, 0.61, 0.36, 1] }}>
            <HeroCard />
          </motion.div>
        </div>

        <div className="trust-strip">
          <p className="trust-strip-label">{t('home.trustLead')}</p>
          <div className="trust-logos">
            {[
              { label: t('home.trust1'), icon: LockKeyhole },
              { label: t('home.trust2'), icon: UserRound },
              { label: t('home.trust3'), icon: ShieldCheck },
              { label: t('home.trust4'), icon: Clock3 },
            ].map(({ label, icon: Icon }) => (
              <span className="trust-logo" key={label}>
                <Icon size={16} strokeWidth={1.6} />
                {label}
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="how">
        <div className="section-head">
          <span className="eyebrow">{t('home.howEyebrow')}</span>
          <h2>{t('home.howTitle')}</h2>
          <p>{t('home.howLead')}</p>
        </div>
        <div className="steps-grid">
          {steps.map(({ icon: Icon, title, body, meta }, index) => (
            <article className="step-card" key={title}>
              <div className="step-top">
                <span className="step-icon"><Icon size={18} strokeWidth={1.7} /></span>
                <span className="step-index">0{index + 1}</span>
              </div>
              <h3>{title}</h3>
              <p>{body}</p>
              <div className="row" style={{ marginTop: 18, color: 'var(--ink-400)', fontSize: 10.5, fontWeight: 600 }}>
                <span className="dot" style={{ background: 'var(--gold-500)' }} />
                {meta}
              </div>
              {index < steps.length - 1 && (
                <span className="step-arrow" aria-hidden="true">
                  <ChevronRight size={13} />
                </span>
              )}
            </article>
          ))}
        </div>
      </section>

      <section className="section-tight" id="track">
        <div className="track-panel">
          <div className="track-panel-aside">
            <div>
              <span className="eyebrow" style={{ color: 'var(--gold-300)' }}>{t('home.trackEyebrow')}</span>
              <h3>{t('home.trackTitle')}</h3>
              <p>{t('home.trackBody')}</p>
            </div>
            <ul className="track-aside-list">
              {[t('home.trackPoint1'), t('home.trackPoint2'), t('home.trackPoint3')].map((point) => (
                <li key={point}>
                  <Check size={13} strokeWidth={2.2} />
                  {point}
                </li>
              ))}
            </ul>
          </div>
          <div className="track-panel-main">
            <h3>{t('track.title')}</h3>
            <p>{t('track.subtitle')}</p>
            <TrackForm />
          </div>
        </div>
      </section>

      <section className="section" id="security">
        <div className="section-head">
          <span className="eyebrow">{t('home.securityEyebrow')}</span>
          <h2>{t('home.securityTitle')}</h2>
          <p>{t('home.securityLead')}</p>
        </div>
        <div className="security-grid">
          {security.map(({ icon: Icon, title, body }) => (
            <article className="security-card" key={title}>
              <span className="step-icon"><Icon size={18} strokeWidth={1.7} /></span>
              <h3>{title}</h3>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="section" id="faq">
        <div className="faq-layout">
          <div className="section-head" style={{ marginBottom: 0 }}>
            <span className="eyebrow">{t('home.faqEyebrow')}</span>
            <h2>{t('home.faqTitle')}</h2>
            <Button variant="ghost" size="sm" style={{ marginTop: 18, paddingInline: 0 }} onClick={() => navigate('/track')} trailingIcon={<Arrow size={14} />}>
              {t('home.ctaSecondary')}
            </Button>
          </div>
          <div className="faq">
            {faqs.map(([question, answer]) => (
              <details key={question}>
                <summary>
                  {question}
                  <span className="faq-plus" aria-hidden="true"><Plus size={13} /></span>
                </summary>
                <p>{answer}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section className="section-tight" style={{ paddingBottom: 72 }}>
        <div className="cta-band">
          <div>
            <h2>{t('home.ctaTitle')}</h2>
            <p>{t('home.ctaBody')}</p>
          </div>
          <div className="cta-band-actions">
            <Button size="lg" onClick={() => navigate('/complaints/new')} trailingIcon={<Arrow size={16} />}>
              {t('home.ctaPrimary')}
            </Button>
            <Button size="lg" variant="light" onClick={() => navigate('/track')}>
              {t('home.ctaSecondary')}
            </Button>
          </div>
        </div>
        <p style={{ marginTop: 14, textAlign: 'center', color: 'var(--ink-400)', fontSize: 11 }}>
          {t('home.reassurance')}
        </p>
      </section>
    </div>
  );
}
