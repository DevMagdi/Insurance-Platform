import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';
import i18n, { applyLanguage } from './i18n';

beforeEach(async () => {
  localStorage.clear();
  await i18n.changeLanguage('ar');
  applyLanguage('ar');
});

afterEach(async () => {
  cleanup();
  localStorage.clear();
  await i18n.changeLanguage('ar');
  applyLanguage('ar');
});
