import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import { ar } from '../locales/ar';
import { en } from '../locales/en';
import arFoundation from './ar.json';
import enFoundation from './en.json';
import { arExtra, enExtra } from './extra';

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

/**
 * The extra bundles are grouped as ``commonExtra``/``ticketsExtra``. Page code
 * legitimately reads both those names and their base namespaces
 * (``common.refresh``, ``tickets.lastUpdate``), so expose each bundle under both
 * keys. Later entries win, which lets the extra copy refine the foundation.
 */
function aliasExtraNamespaces(tree: TranslationTree): TranslationTree {
  const expanded: TranslationTree = { ...tree };
  for (const [key, value] of Object.entries(tree)) {
    if (!key.endsWith('Extra') || !isTree(value)) continue;
    const base = key.slice(0, -'Extra'.length);
    const current = expanded[base];
    expanded[base] = isTree(current) ? mergeDeep(current, value) : value;
  }
  return expanded;
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
    ar: { translation: aliasExtraNamespaces(mergeDeep(mergeDeep(arFoundation as TranslationTree, ar as TranslationTree), arExtra as TranslationTree)) },
    en: { translation: aliasExtraNamespaces(mergeDeep(mergeDeep(enFoundation as TranslationTree, en as TranslationTree), enExtra as TranslationTree)) },
  },
  lng: initial,
  fallbackLng: 'ar',
  interpolation: { escapeValue: false },
});

applyLanguage(initial);

export default i18n;
