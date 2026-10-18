import { useState } from 'react';
import { Link, NavLink } from 'react-router-dom';
import { Menu, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/Button';
import { setLocale } from '../../i18n';
import { useTheme } from '../../store/theme';
import { usePlatformBrand } from '../Branding';

export function SiteHeader() {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const brand = usePlatformBrand();
  const theme = useTheme((s) => s.theme);
  const toggle = useTheme((s) => s.toggle);
  const links = [['/#features', t('nav.features')], ['/#how', t('nav.how')], ['/#security', t('landing.trustTitle')]];
  return <header className="sticky top-0 z-40 border-b border-white/10 bg-navy/90 text-white backdrop-blur-xl">
    <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
      <Link to="/" className="flex items-center gap-3" aria-label={brand.platform_name}><img className="size-10 rounded-xl" src={brand.logo_url} alt="" /><span className="font-bold leading-tight">{brand.platform_name}<span className="block text-[10px] font-medium tracking-widest text-emerald-300">INSURANCE</span></span></Link>
      <nav className="hidden items-center gap-8 lg:flex">{links.map(([to, label]) => <a key={to} href={to} className="text-sm text-slate-300 transition hover:text-white">{label}</a>)}</nav>
      <div className="hidden items-center gap-3 lg:flex"><button className="rounded-lg px-3 py-2 text-sm text-slate-300 hover:text-white" onClick={() => setLocale(i18n.language === 'ar' ? 'en' : 'ar')}>{i18n.language === 'ar' ? 'EN' : 'عربي'}</button><button className="rounded-lg px-2 py-2 text-slate-300" aria-label="Toggle color theme" onClick={toggle}>{theme === 'dark' ? '☼' : '☾'}</button><NavLink to="/auth/staff"><Button variant="secondary" size="sm">{t('nav.login')}</Button></NavLink><a href="#demo"><Button size="sm">{t('nav.signup')}</Button></a></div>
      <button className="lg:hidden" aria-label={open ? 'إغلاق القائمة' : 'فتح القائمة'} onClick={() => setOpen(!open)}>{open ? <X/> : <Menu/>}</button>
    </div>
    {open && <nav className="border-t border-white/10 px-5 py-4 lg:hidden">{links.map(([to,label]) => <a key={to} href={to} onClick={() => setOpen(false)} className="block py-3 text-slate-200">{label}</a>)}<div className="flex gap-3 pt-3"><NavLink to="/auth/staff"><Button variant="secondary">{t('nav.login')}</Button></NavLink><a href="#demo"><Button>{t('nav.signup')}</Button></a></div></nav>}
  </header>;
}
