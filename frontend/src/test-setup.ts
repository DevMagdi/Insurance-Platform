import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';
import i18n, { applyLanguage } from './i18n';

function clearSessionCookies(): void {
  document.cookie = 'csrf_token=; Path=/; Max-Age=0';
  document.cookie = 'csrf_token=; Path=/api; Max-Age=0';
}

beforeEach(async () => {
  localStorage.clear();
  clearSessionCookies();
  await i18n.changeLanguage('ar');
  applyLanguage('ar');
});

afterEach(async () => {
  cleanup();
  localStorage.clear();
  clearSessionCookies();
  await i18n.changeLanguage('ar');
  applyLanguage('ar');
});
