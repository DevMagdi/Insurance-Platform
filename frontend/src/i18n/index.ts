import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import ar from './ar.json';
import en from './en.json';

const saved = typeof localStorage !== 'undefined' ? localStorage.getItem('locale') : null;
const initial = saved || 'ar';

i18n.use(initReactI18next).init({
  resources: { ar: { translation: ar }, en: { translation: en } },
  lng: initial,
  fallbackLng: 'ar',
  interpolation: { escapeValue: false },
});

export function setLocale(lng: string) {
  i18n.changeLanguage(lng);
  if (typeof localStorage !== 'undefined') localStorage.setItem('locale', lng);
  document.documentElement.lang = lng;
  document.documentElement.dir = lng.startsWith('ar') ? 'rtl' : 'ltr';
}

setLocale(initial);
export default i18n;
