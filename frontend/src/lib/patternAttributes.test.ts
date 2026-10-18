import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * A browser compiles an HTML `pattern` attribute as
 * `new RegExp('^(?:' + pattern + ')$', 'v')` — the `v` (unicodeSets) flag, not
 * the `u` flag a JS literal uses. Under `v` a literal `/` or `-` inside a
 * character class is a syntax error: the browser logs
 * "Invalid regular expression: Invalid character in character class", drops the
 * constraint, and the field silently accepts anything.
 *
 * The logo path field shipped exactly that (`^/(?!/)[A-Za-z0-9._/-]+$`), so this
 * test compiles every inline `pattern` attribute in the source tree the way a
 * browser does. Shared sources live in `lib/validation.ts` and are covered by
 * `validation.test.ts`; prefer those constants over a new inline literal.
 */
const SOURCE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

function tsxFiles(directory: string, found: string[] = []): string[] {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) tsxFiles(path, found);
    else if (entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx')) found.push(path);
  }
  return found;
}

interface PatternAttribute { file: string; line: number; pattern: string; }

function inlinePatterns(): PatternAttribute[] {
  const attributes: PatternAttribute[] = [];
  // Matches pattern="…" and pattern={"…"} / pattern={'…'} literals only;
  // `pattern={SOME_CONSTANT}` is verified where the constant is defined.
  const literal = /pattern\s*=\s*(?:"([^"]*)"|\{\s*(["'])((?:(?!\2)[^\\]|\\.)*)\2\s*\})/g;
  for (const file of tsxFiles(SOURCE_ROOT)) {
    const lines = readFileSync(file, 'utf8').split('\n');
    lines.forEach((text, index) => {
      for (const match of text.matchAll(literal)) {
        const pattern = match[1] ?? match[3];
        if (pattern === undefined) continue;
        attributes.push({
          file: relative(SOURCE_ROOT, file),
          line: index + 1,
          // A JSX attribute string keeps its backslashes; a JS string literal
          // does not, so unescape it the way the parser would.
          pattern: match[1] !== undefined ? pattern : pattern.replace(/\\(.)/g, '$1'),
        });
      }
    });
  }
  return attributes;
}

describe('HTML pattern attributes compile in a browser (unicodeSets `v` flag)', () => {
  const attributes = inlinePatterns();

  it('finds the inline attributes in the source tree', () => {
    // Guards the scan itself: if the regex stops matching, the test would pass
    // vacuously while a broken pattern ships again.
    expect(attributes.length).toBeGreaterThan(0);
  });

  it.each(attributes.map((item) => [`${item.file}:${item.line} ${item.pattern}`, item] as const))(
    'compiles %s',
    (_label, attribute) => {
      let compiled: RegExp;
      try {
        compiled = new RegExp(`^(?:${attribute.pattern})$`, 'v');
      } catch (cause) {
        throw new Error(
          `${attribute.file}:${attribute.line} uses a pattern the browser rejects: ${String(cause)}. `
          + 'Escape `/` and `-` inside character classes, or use a shared source from lib/validation.ts.',
          { cause },
        );
      }
      expect(compiled).toBeInstanceOf(RegExp);
    },
  );
});
