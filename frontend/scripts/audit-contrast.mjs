#!/usr/bin/env node
/*
 * Audit WCAG contrast for semantic text-on-surface pairings in both themes.
 * Reads token values from src/styles/brand-tokens.css. Not wired into CI;
 * run ad hoc: node scripts/audit-contrast.mjs
 *
 * Thresholds: AA normal text 4.5, AA large/UI text 3.0.
 */
import Color from 'color';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const css = readFileSync(join(ROOT, 'src/styles/brand-tokens.css'), 'utf8');

const darkStart = css.search(/^\.dark\s*\{/m);
function parse(block) {
  const out = {};
  for (const m of block.matchAll(/--brand-color-([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})/g)) {
    out[m[1]] = m[2];
  }
  return out;
}
const light = parse(css.slice(0, darkStart));
const dark = { ...light, ...parse(css.slice(darkStart)) }; // dark inherits unspecified light values

// [text token, surface token, label, threshold]
const PAIRS = [
  ['foreground', 'background', 'body text / page', 4.5],
  ['foreground', 'card', 'body text / card', 4.5],
  ['foreground', 'panel', 'body text / panel', 4.5],
  ['muted-foreground', 'background', 'muted text / page', 4.5],
  ['muted-foreground', 'card', 'muted text / card', 4.5],
  ['muted-foreground', 'panel', 'muted text / panel', 4.5],
  ['muted-foreground', 'muted', 'muted text / muted chip', 4.5],
  ['primary', 'background', 'link / page', 4.5],
  ['primary', 'card', 'link / card', 4.5],
  ['primary', 'selected-bg', 'active nav / selected', 4.5],
  ['success', 'card', 'success text / card', 4.5],
  ['danger', 'card', 'danger text / card', 4.5],
  ['warning', 'card', 'warning text / card', 4.5],
  ['info', 'card', 'info text / card', 4.5],
  ['info', 'background', 'info text / page', 4.5],
  // Badge text on tinted backgrounds
  ['success-text', 'success-bg', 'success badge', 4.5],
  ['error-text', 'error-bg', 'error badge', 4.5],
  ['warning-text', 'warning-bg', 'warning badge', 4.5],
  ['info-text', 'info-bg', 'info badge', 4.5],
];

// Solid button: foreground-on-fill. Light accents use white text; dark accents
// are bright, so palette.ts sets their foreground to near-black (#1A1816).
const BUTTON_FILLS = ['primary', 'danger', 'success'];

function ratio(a, b) {
  return Color(a).contrast(Color(b));
}

function run(name, map, accentFg) {
  console.log(`\n=== ${name} ===`);
  const fails = [];
  for (const [t, s, label, thr] of PAIRS) {
    const tc = map[t];
    const sc = map[s];
    if (!tc || !sc) {
      console.log(`  ?  ${label.padEnd(28)} missing token (${t}=${tc} ${s}=${sc})`);
      continue;
    }
    const r = ratio(tc, sc);
    const ok = r >= thr;
    if (!ok) fails.push(`${label}: ${r.toFixed(2)} (need ${thr})  [${t} ${tc} on ${s} ${sc}]`);
    console.log(`  ${ok ? 'ok ' : 'XX '} ${label.padEnd(28)} ${r.toFixed(2)}  (${t} on ${s})`);
  }
  for (const s of BUTTON_FILLS) {
    const sc = map[s];
    if (!sc) continue;
    const r = ratio(accentFg, sc);
    const ok = r >= 4.5;
    const label = `${s} button text`;
    if (!ok) fails.push(`${label}: ${r.toFixed(2)} (need 4.5)  [${accentFg} on ${s} ${sc}]`);
    console.log(`  ${ok ? 'ok ' : 'XX '} ${label.padEnd(28)} ${r.toFixed(2)}  (${accentFg} on ${s})`);
  }
  return fails;
}

const lf = run('LIGHT', light, '#ffffff');
const df = run('DARK', dark, '#1A1816');

console.log('\n========== FAILURES ==========');
if (!lf.length && !df.length) console.log('None. All pairings pass WCAG AA.');
for (const f of lf) console.log(`LIGHT  ${f}`);
for (const f of df) console.log(`DARK   ${f}`);
