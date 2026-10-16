import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import { ar } from '../locales/ar';
import { en } from '../locales/en';
import arFoundation from './ar.json';
import enFoundation from './en.json';

export type AppLanguage = 'ar' | 'en';

type TranslationTree = { [key: string]: string | TranslationTree };

function isTree(value: unknown): value is TranslationTree {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function mergeDeep(base: TranslationTree, override: TranslationTree): TranslationTree {
  const merged: TranslationTree = { ...base };
  for (const [key, value] of Object.entries(override)) {
    const current = merged[key];
    merged[key] = isTree(current) && isTree(value) ? mergeDeep(current, value) : value;
  }
  return merged;
}

export function applyLanguage(lng: AppLanguage): void {
  document.documentElement.lang = lng;
  document.documentElement.dir = lng === 'ar' ? 'rtl' : 'ltr';
  try {
    localStorage.setItem('insurance-portal:lang', lng);
    localStorage.setItem('locale', lng);
  } catch {
    // Storage unavailable; language still applies for this session.
  }
}

export function initialLanguage(): AppLanguage {
  try {
    return localStorage.getItem('insurance-portal:lang') === 'en' || localStorage.getItem('locale') === 'en'
      ? 'en'
      : 'ar';
  } catch {
    return 'ar';
  }
}

export function setLocale(lng: string): void {
  const language: AppLanguage = lng.startsWith('ar') ? 'ar' : 'en';
  void i18n.changeLanguage(language);
  applyLanguage(language);
}

const initial = initialLanguage();

void i18n.use(initReactI18next).init({
  resources: {
    ar: { translation: mergeDeep(arFoundation as TranslationTree, ar as TranslationTree) },
    en: { translation: mergeDeep(enFoundation as TranslationTree, en as TranslationTree) },
  },
  lng: initial,
  fallbackLng: 'ar',
  interpolation: { escapeValue: false },
});

applyLanguage(initial);

export default i18n;
