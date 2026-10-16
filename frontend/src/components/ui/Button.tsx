import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cn } from '../../lib/utils';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'sm' | 'md' | 'lg'; children: ReactNode };
export function Button({ variant = 'primary', size = 'md', className, children, ...props }: Props) {
  const variants = { primary: 'bg-gold text-navy hover:bg-amber-300 shadow-lg shadow-gold/15', secondary: 'border border-white/15 bg-white/5 text-white hover:bg-white/10', ghost: 'text-slate-300 hover:bg-white/10 hover:text-white', danger: 'bg-rose-600 text-white hover:bg-rose-500' };
  const sizes = { sm: 'px-3 py-2 text-sm', md: 'px-5 py-3 text-sm', lg: 'px-7 py-4 text-base' };
  return <button className={cn('inline-flex items-center justify-center gap-2 rounded-xl font-semibold transition duration-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gold disabled:cursor-not-allowed disabled:opacity-50', variants[variant], sizes[size], className)} {...props}>{children}</button>;
}
