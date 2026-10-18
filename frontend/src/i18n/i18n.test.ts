import { describe, expect, it } from 'vitest';
import i18n from './index';

/**
 * Regression guard for keys that page code reads from both the ``*Extra``
 * bundles and their base namespaces. A missing key renders as the literal key
 * ("common.refresh") in the UI, which is exactly the bug these tests cover.
 */
const SHARED_KEYS = [
  'common.refresh',
  'commonExtra.refresh',
  'tickets.lastUpdate',
  'tickets.emptyHint',
  'tickets.showing',
  'ticketsExtra.lastUpdate',
  'detailExtra.csatDesc',
  'profileExtra.savedAt',
  'trackExtra.formTitle',
];

describe('i18n resources', () => {
  for (const lng of ['ar', 'en'] as const) {
    it(`resolves every shared key in ${lng}`, async () => {
      await i18n.changeLanguage(lng);
      for (const key of SHARED_KEYS) {
        const copy = i18n.t(key);
        expect(copy, `${lng}:${key}`).toBeTruthy();
        expect(copy, `${lng}:${key}`).not.toBe(key);
      }
    });
  }

  it('keeps Arabic and English key trees in step', async () => {
    const flatten = (value: unknown, prefix = '', out: string[] = []): string[] => {
      if (typeof value === 'object' && value !== null) {
        for (const [key, child] of Object.entries(value)) {
          flatten(child, prefix ? `${prefix}.${key}` : key, out);
        }
      } else {
        out.push(prefix);
      }
      return out;
    };
    await i18n.changeLanguage('ar');
    const ar = new Set(flatten(i18n.getResourceBundle('ar', 'translation')));
    await i18n.changeLanguage('en');
    const en = new Set(flatten(i18n.getResourceBundle('en', 'translation')));
    expect([...ar].filter((key) => !en.has(key))).toEqual([]);
    expect([...en].filter((key) => !ar.has(key))).toEqual([]);
  });
});

/**
 * Regression guard for the canonical Arabic mobile label. The customer-facing
 * field was labelled "الجوال"/"رقم الجوال" and had to become "رقم الموبايل"
 * consistently across registration (login), the complaint wizard and the
 * profile, with "Mobile number" as the English counterpart.
 */
describe('mobile field labels', () => {
  const MOBILE_KEYS = ['auth.mobile', 'wizard.mobile', 'profile.mobile'];

  it('labels every customer mobile field "رقم الموبايل" in Arabic', async () => {
    await i18n.changeLanguage('ar');
    for (const key of MOBILE_KEYS) {
      expect(i18n.t(key), `ar:${key}`).toBe('رقم الموبايل');
    }
    expect(i18n.t('validation.mobile')).toContain('رقم موبايل مصري');
  });

  it('labels every customer mobile field "Mobile number" in English', async () => {
    await i18n.changeLanguage('en');
    for (const key of MOBILE_KEYS) {
      expect(i18n.t(key), `en:${key}`).toBe('Mobile number');
    }
  });

  it('never renders the deprecated wording "جوال" anywhere', async () => {
    for (const lng of ['ar', 'en'] as const) {
      await i18n.changeLanguage(lng);
      const bundle = JSON.stringify(i18n.getResourceBundle(lng, 'translation'));
      expect(bundle, `${lng} bundle`).not.toContain('جوال');
    }
  });
});
