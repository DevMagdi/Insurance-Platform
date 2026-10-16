import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import { ar } from './locales/ar';
import { en } from './locales/en';

export type AppLanguage = 'ar' | 'en';

export function applyLanguage(lng: AppLanguage): void {
  document.documentElement.lang = lng;
  document.documentElement.dir = lng === 'ar' ? 'rtl' : 'ltr';
  try {
    localStorage.setItem('insurance-portal:lang', lng);
  } catch {
    // Storage unavailable; language still applies for this session.
  }
}

export function initialLanguage(): AppLanguage {
  try {
    return localStorage.getItem('insurance-portal:lang') === 'en' ? 'en' : 'ar';
  } catch {
    return 'ar';
  }
}

void i18n.use(initReactI18next).init({
  resources: { ar: { translation: ar }, en: { translation: en } },
  lng: initialLanguage(),
  fallbackLng: 'ar',
  interpolation: { escapeValue: false },
});

export default i18n;
