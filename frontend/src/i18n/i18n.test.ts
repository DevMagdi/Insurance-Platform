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
