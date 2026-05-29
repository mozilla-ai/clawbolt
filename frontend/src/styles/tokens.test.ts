/**
 * Design-token guardrails. Three independent checks keep the token system
 * coherent so the failure that let `bg-muted` (an undefined token that compiled
 * to nothing) ship silently cannot recur:
 *
 *  1. undefined-token guard -- every semantic color utility used in the app
 *     resolves to a real token (our @theme or HeroUI's).
 *  2. drift guard -- palette.ts (HeroUI's source) and brand-tokens.css (the app
 *     utility source) agree on the shared semantic colors.
 *  3. generated-file freshness -- heroui-tokens.generated.css matches what
 *     `npm run generate:tokens` would produce from palette.ts.
 */
/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import Color from 'color';
import { lightColors, darkColors } from './palette';

// CSS files are read verbatim via fs: a `?raw` import would be transformed by
// the @tailwindcss/vite plugin (which compiles away @theme and token decls).
const STYLES = dirname(fileURLToPath(import.meta.url));
const brandTokensCss = readFileSync(join(STYLES, 'brand-tokens.css'), 'utf8');
const indexCss = readFileSync(join(STYLES, '..', 'index.css'), 'utf8');
const generatedCss = readFileSync(join(STYLES, 'heroui-tokens.generated.css'), 'utf8');

// All app source files as raw text (excludes tests and generated output).
const sourceModules = import.meta.glob('../**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const sourceFiles: [string, string][] = Object.entries(sourceModules).filter(
  ([path]) => !/\.test\.(tsx|ts)$/.test(path) && !/generated/.test(path),
);

// ---------------------------------------------------------------------------
// 1. Undefined-token guard
// ---------------------------------------------------------------------------

/** Color token roots defined by our @theme block in index.css (e.g. "muted", "primary-600"). */
function themeColorRoots(): Set<string> {
  const theme = indexCss.slice(indexCss.indexOf('@theme'));
  const roots = new Set<string>();
  for (const m of theme.matchAll(/--color-([a-z0-9-]+):/g)) {
    if (m[1]) roots.add(m[1]);
  }
  return roots;
}

/** Color roots HeroUI's plugin registers (unprefixed Tailwind color utilities). */
function herouiColorRoots(): Set<string> {
  const roots = new Set<string>([
    'background',
    'foreground',
    'divider',
    'focus',
    'overlay',
    'content1',
    'content2',
    'content3',
    'content4',
  ]);
  for (const fam of ['default', 'primary', 'secondary', 'success', 'warning', 'danger']) {
    roots.add(fam);
    roots.add(`${fam}-foreground`);
    for (const step of [50, 100, 200, 300, 400, 500, 600, 700, 800, 900]) {
      roots.add(`${fam}-${step}`);
    }
  }
  return roots;
}

const TAILWIND_BUILTIN = ['white', 'black', 'transparent', 'current', 'inherit'];

// Tailwind color-bearing utility prefixes.
const COLOR_PREFIXES = [
  'bg',
  'text',
  'border',
  'ring',
  'fill',
  'stroke',
  'from',
  'to',
  'via',
  'divide',
  'outline',
  'decoration',
  'accent',
  'caret',
  'placeholder',
];

it('every semantic color utility in the app resolves to a defined token', () => {
  const defined = new Set<string>([
    ...themeColorRoots(),
    ...herouiColorRoots(),
    ...TAILWIND_BUILTIN,
  ]);
  // Family first-segments we treat as "ours": anything else (text-sm, border-t,
  // bg-cover, ...) is a layout/typography utility, not a color token.
  const families = new Set([...defined].map((r) => r.split('-')[0]));

  const prefixRe = new RegExp(
    `\\b(?:${COLOR_PREFIXES.join('|')})-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)`,
    'g',
  );

  const offenders = new Set<string>();
  for (const [path, text] of sourceFiles) {
    for (const m of text.matchAll(prefixRe)) {
      const root = m[1];
      if (!root) continue;
      const head = root.split('-')[0];
      if (!head || !families.has(head)) continue; // not a color token utility
      if (defined.has(root)) continue;
      offenders.add(`${path}: ${m[0]}`);
    }
  }

  expect(
    [...offenders],
    `Undefined color-token utilities (no backing --color-* or HeroUI token):\n${[...offenders].join('\n')}`,
  ).toEqual([]);
});

// ---------------------------------------------------------------------------
// 2. Drift guard: palette.ts vs brand-tokens.css
// ---------------------------------------------------------------------------

/** Parse `--brand-color-NAME: #hex;` from a CSS block, lowercased. */
function brandHexes(css: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of css.matchAll(/--brand-color-([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})/g)) {
    if (m[1] && m[2]) out.set(m[1], m[2].toLowerCase());
  }
  return out;
}

function sameColor(a: string, b: string): boolean {
  return Color(a).hex().toLowerCase() === Color(b).hex().toLowerCase();
}

it('palette.ts and brand-tokens.css agree on shared semantic colors', () => {
  const darkStart = brandTokensCss.search(/^\.dark\s*\{/m);
  const light = brandHexes(brandTokensCss.slice(0, darkStart));
  const dark = brandHexes(brandTokensCss.slice(darkStart));

  // [palette value (light, dark), brand-tokens key]
  const pairs: [string, string, string][] = [
    [lightColors.background, darkColors.background, 'background'],
    [lightColors.foreground, darkColors.foreground, 'foreground'],
    [lightColors.content1, darkColors.content1, 'card'],
    [lightColors.content2, darkColors.content2, 'panel'],
    [lightColors.divider, darkColors.divider, 'border'],
    [lightColors.primary.DEFAULT, darkColors.primary.DEFAULT, 'primary'],
    [lightColors.success.DEFAULT, darkColors.success.DEFAULT, 'success'],
    [lightColors.danger.DEFAULT, darkColors.danger.DEFAULT, 'danger'],
  ];

  const mismatches: string[] = [];
  for (const [lp, dp, key] of pairs) {
    const lb = light.get(key);
    const db = dark.get(key);
    if (lb && !sameColor(lp, lb)) mismatches.push(`light ${key}: palette ${lp} != --brand-color-${key} ${lb}`);
    if (db && !sameColor(dp, db)) mismatches.push(`dark ${key}: palette ${dp} != --brand-color-${key} ${db}`);
  }

  expect(mismatches, `palette.ts drifted from brand-tokens.css:\n${mismatches.join('\n')}`).toEqual([]);
});

// ---------------------------------------------------------------------------
// 3. Generated-file freshness
// ---------------------------------------------------------------------------

function channels(hex: string): string {
  const [h, s, l] = Color(hex).hsl().round(2).array() as [number, number, number];
  const clean = (n: number): string => String(Number(Number(n).toFixed(2)));
  return `${clean(h)} ${clean(s)}% ${clean(l)}%`;
}

it('heroui-tokens.generated.css is up to date with palette.ts', () => {
  const families = ['default', 'primary', 'secondary', 'success', 'warning', 'danger'] as const;
  const bases = ['background', 'foreground', 'divider', 'focus', 'content1', 'content2', 'content3', 'content4'] as const;
  const missing: string[] = [];

  const check = (suffix: string, hex: string): void => {
    if (!generatedCss.includes(`--brand-h-${suffix}: ${channels(hex)};`)) {
      missing.push(`--brand-h-${suffix}: ${channels(hex)};`);
    }
    if (!generatedCss.includes(`--heroui-${suffix}: var(--brand-h-${suffix});`)) {
      missing.push(`--heroui-${suffix}: var(--brand-h-${suffix});`);
    }
  };

  for (const theme of [lightColors, darkColors]) {
    for (const base of bases) check(base, theme[base]);
    for (const fam of families) {
      const scale = theme[fam] as unknown as Record<string, string>;
      for (const [k, v] of Object.entries(scale)) {
        const suffix = k === 'DEFAULT' ? fam : k === 'foreground' ? `${fam}-foreground` : `${fam}-${k}`;
        check(suffix, v);
      }
    }
  }

  expect(
    [...new Set(missing)].slice(0, 20),
    `heroui-tokens.generated.css is stale. Run: npm run generate:tokens`,
  ).toEqual([]);
});
