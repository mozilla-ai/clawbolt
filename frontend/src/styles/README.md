# Clawbolt Design System (frontend implementation)

This is the engineering reference for the token system. For the visual spec
(palette intent, typography, spacing, motion) see the repo-root `DESIGN.md`,
which is canonical. This file documents **how** the tokens are wired so changes
stay in one place and components stay override-free.

## Principle

**Design tokens are the single source of truth. Components consume tokens; they
do not hard-code colors.** A rebrand or theme change should be a token edit, not
a sweep through component files. HeroUI components are themed through the same
tokens at runtime, so there is no second palette to keep in sync by hand.

## The layers

```
palette.ts ──────────────► hero.ts (HeroUI plugin colors, build time)
   │                            │
   │  npm run generate:tokens   │  compiles to --heroui-* (HSL channels)
   ▼                            ▼
heroui-tokens.generated.css ── rebinds --heroui-* → var(--brand-h-*)  (runtime)
                                  ▲
brand-tokens.css ── --brand-color-* (hex) ─┘   ← single source for app utilities
   │
   ▼
index.css @theme ── maps --color-* → var(--brand-color-*)  → Tailwind utilities
                    (bg-primary, text-foreground, ...)
```

- **`brand-tokens.css`** — the source of truth for app-level color values, as
  `--brand-color-*` hex. Light values at `:root`, dark overrides under
  `html:where(.dark, .dark *)`. Also carries the Starlight docs alias layer.
- **`index.css` `@theme`** — declares Tailwind utility names and points each
  `--color-*` at a `--brand-color-*`. This is what generates `bg-primary`,
  `text-muted-foreground`, etc. Values live in `brand-tokens.css`, so a palette
  change is one edit.
- **`palette.ts`** — the source of truth for the colors HeroUI components render
  with. `hero.ts` imports it; the HeroUI plugin compiles it to `--heroui-*`
  HSL-channel variables.
- **`heroui-tokens.generated.css`** (generated, do not hand-edit) — rebinds
  every `--heroui-*` variable to a `var(--brand-h-*)` token so HeroUI components
  follow our tokens at runtime (theme switch / live override), not build-frozen
  hex. Imported in `index.css` after `@plugin "./hero.ts"` so it wins.

## How to change a color

1. Edit the hex in **`brand-tokens.css`** (light `:root` and the dark block).
2. If the color is one HeroUI components use (primary, success, danger, warning,
   the surfaces, etc.), make the matching edit in **`palette.ts`** and run
   **`npm run generate:tokens`**.
3. Run `npm test` — `src/styles/tokens.test.ts` enforces that the two stay in
   sync and that the generated file is fresh.

For a full rebrand, those two files plus a regenerate cover the whole app,
including HeroUI components.

## Guardrails (`src/styles/tokens.test.ts`)

1. **Undefined-token guard** — every semantic color utility used in the app
   (`bg-muted`, `text-info`, ...) must resolve to a real token. This is what
   would have caught `bg-muted` / `bg-info` / `text-info` shipping as no-ops.
2. **Drift guard** — `palette.ts` and `brand-tokens.css` agree on shared
   semantic colors.
3. **Generated-file freshness** — `heroui-tokens.generated.css` matches what the
   generator produces from `palette.ts`. Forgot to regenerate? The test fails.

Ad-hoc contrast check: `node scripts/audit-contrast.mjs` reports WCAG ratios for
every semantic text-on-surface pairing in both themes.

## Rules for components

- **Use semantic utilities only**: `bg-card`, `bg-panel`, `bg-muted`,
  `text-foreground`, `text-muted-foreground`, `text-primary`, `border-border`,
  the `*-bg` / `*-text` state pairs, etc. Never a raw Tailwind palette color
  (`bg-gray-200`, `text-red-500`) and never a hex literal. Brand logos/3rd-party
  icons are the only exception.
- **Text on a colored fill** uses the fill's foreground token, not `text-white`.
  `bg-primary` → `text-primary-foreground` (white in light, near-black in dark,
  so amber stays readable in both themes). Same for badges via the `*-bg` /
  `*-text` pairs, which are tuned to pass WCAG AA.
- **Style HeroUI via props / `classNames` with token utilities**, not by
  overriding its internals. The tokens already flow into HeroUI, so a
  `<Button color="primary" />` is themed correctly with no extra CSS.
- **Contrast**: secondary text must use `text-muted-foreground` (AA-tuned), not
  an ad-hoc opacity on `foreground`. Colored status text on a neutral chip is a
  trap (low contrast); use the `*-bg` + `*-text` pair instead.

## Responsiveness conventions

- Mobile-first: base classes target small screens; add `sm:` / `md:` / `lg:` to
  scale up. Multi-column grids start `grid-cols-1` and gain columns at `md:`.
- The app shell (`layouts/AppShell.tsx`) is the canonical pattern: off-canvas
  sidebar under `md` with a mobile header; static sidebar at `md+`.
- Inputs use `text-base` on mobile (`index.css` forces 16px under 768px) to stop
  iOS Safari auto-zoom. Keep tap targets comfortable (tradespeople, gloves).
- Wrap any `<table>` in `overflow-x-auto`. Avoid fixed pixel widths that can
  exceed the viewport; prefer `max-w-*` + fluid widths.
- Prefer CSS-driven state (transforms, `hidden`/`md:block`) over JS layout
  swaps to avoid layout shift and full re-renders.
