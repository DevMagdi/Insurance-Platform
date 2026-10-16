import type { ReactNode } from 'react';
export function SectionTitle({ eyebrow, title, description }: { eyebrow?: string; title: string; description?: ReactNode }) {
  return <div className="mx-auto mb-12 max-w-2xl text-center"><p className="mb-3 text-xs font-bold uppercase tracking-[.25em] text-emerald-500">{eyebrow}</p><h2 className="text-3xl font-bold tracking-tight text-slate-900 dark:text-white md:text-4xl">{title}</h2>{description && <p className="mt-4 leading-8 text-slate-600 dark:text-slate-400">{description}</p>}</div>;
}
