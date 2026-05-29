#!/usr/bin/env node
/*
 * Generate src/styles/heroui-tokens.generated.css from src/styles/palette.ts.
 *
 * The output rebinds every HeroUI color variable (`--heroui-*`) to a
 * `var(--brand-h-*)` token, and defines those `--brand-h-*` tokens as HSL
 * channel triples derived from the palette. This makes HeroUI components follow
 * our design tokens at runtime: switching the `.dark` class (or live-overriding
 * a `--brand-h-*` var) restyles HeroUI without rebuilding.
 *
 * Channels are produced with the same `color` library and rounding HeroUI's own
 * plugin uses (`Color(hex).hsl().round(2).array()`), so the generated values are
 * byte-identical to what HeroUI would compile from the same hex: the rebind is a
 * visual no-op until you change a token.
 *
 * Run: npm run generate:tokens   (CI guard: scripts/check-tokens, via tokens.test.ts)
 */
import { buildSync } from 'esbuild';
import Color from 'color';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const PALETTE_TS = join(ROOT, 'src/styles/palette.ts');
const OUT_CSS = join(ROOT, 'src/styles/heroui-tokens.generated.css');

async function loadPalette() {
  const bundled = buildSync({
    entryPoints: [PALETTE_TS],
    bundle: true,
    format: 'esm',
    write: false,
    platform: 'node',
  }).outputFiles[0].text;
  const tmp = join(mkdtempSync(join(tmpdir(), 'palette-')), 'palette.mjs');
  writeFileSync(tmp, bundled);
  return import(pathToFileURL(tmp).href);
}

/** hex -> "H S% L%" channel triple, matching HeroUI's Color(hex).hsl().round(2).array(). */
function channels(hex) {
  const [h, s, l] = Color(hex).hsl().round(2).array();
  const clean = (n) => String(Number(Number(n).toFixed(2)));
  return `${clean(h)} ${clean(s)}% ${clean(l)}%`;
}

const FAMILIES = ['default', 'primary', 'secondary', 'success', 'warning', 'danger'];
const BASE = ['background', 'foreground', 'divider', 'focus', 'content1', 'content2', 'content3', 'content4'];

/** Walk a ThemeColors object -> { herouiSuffix: hex } for every color HeroUI emits. */
function flatten(theme) {
  const out = {};
  for (const key of BASE) out[key] = theme[key];
  for (const fam of FAMILIES) {
    const scale = theme[fam];
    for (const [k, v] of Object.entries(scale)) {
      if (k === 'DEFAULT') out[fam] = v;
      else if (k === 'foreground') out[`${fam}-foreground`] = v;
      else out[`${fam}-${k}`] = v;
    }
  }
  // HeroUI also emits an overlay channel (always black) that the palette doesn't model.
  out['overlay'] = '#000000';
  return out;
}

function emitBlock(selector, flat, indent = '  ') {
  const suffixes = Object.keys(flat);
  const brand = suffixes
    .map((s) => `${indent}--brand-h-${s}: ${channels(flat[s])};`)
    .join('\n');
  const hero = suffixes
    .map((s) => `${indent}--heroui-${s}: var(--brand-h-${s});`)
    .join('\n');
  return `${selector} {\n${brand}\n\n${hero}\n}`;
}

const palette = await loadPalette();
const light = flatten(palette.lightColors);
const dark = flatten(palette.darkColors);

const header = `/*
 * GENERATED FILE -- do not edit by hand.
 * Source: src/styles/palette.ts  |  Regenerate: npm run generate:tokens
 *
 * Rebinds HeroUI's --heroui-* variables to --brand-h-* design tokens so HeroUI
 * components follow our tokens at runtime. Imported after @plugin "./hero.ts"
 * in index.css so these overrides win over HeroUI's build-time defaults.
 */`;

const css = [
  header,
  '',
  emitBlock(':root,\n[data-theme="light"],\n.light', light),
  '',
  emitBlock('.dark', dark),
  '',
].join('\n');

writeFileSync(OUT_CSS, css);
console.log(`Wrote ${OUT_CSS}`);
console.log(`  light: ${Object.keys(light).length} colors, dark: ${Object.keys(dark).length} colors`);
