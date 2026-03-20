# Design System -- Clawbolt

## Product Context
- **What this is:** AI assistant for the trades. Users manage their contracting business via messaging (Telegram, webchat): estimates, client records, job photos, voice memos, file organization.
- **Who it's for:** Contractors, handymen, tradespeople. Often on phones at job sites, not sitting at desks.
- **Space/industry:** Field service management / trades SaaS. Peers: Housecall Pro, Jobber, ServiceTitan, ContractorPlus.
- **Project type:** Web app dashboard + chat interface.

## Aesthetic Direction
- **Direction:** Industrial/Utilitarian with warmth
- **Decoration level:** Expressive (gradient backgrounds, frosted glass cards, subtle grain texture for depth and sophistication. Reference: octonous.ai for the level of polish expected.)
- **Mood:** Reliable workshop tool with polish. Confident, warm, practical, but visually sophisticated. The kind of product that feels premium but not corporate.
- **Logo:** Existing logo (hardware bolt + lobster claw). Do NOT replace with lightning bolt or other icons. Use `/clawbolt.png` asset.
- **Reference sites:** housecallpro.com, servicetitan.com, contractorplus.app, octonous.ai (sibling mozilla.ai project, reference for visual polish)

## Typography
- **Display/Hero:** Outfit 600-700. Geometric, modern utility feel. Has personality without being quirky. Use for page titles, stat values, brand text.
- **Body:** DM Sans 400-600. Warm, readable, slightly rounded terminals. Use for all body text, labels, UI elements.
- **UI/Labels:** DM Sans 500 (same as body, medium weight)
- **Data/Tables:** DM Sans with `font-variant-numeric: tabular-nums`. Numbers align in columns.
- **Code:** JetBrains Mono 400-500
- **Loading:** Google Fonts CDN. `family=Outfit:wght@400;500;600;700&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@400;500`
- **Scale:**
  - Page title: 20px / Outfit 600
  - Section header: 15px / DM Sans 600
  - Body: 15px / DM Sans 400
  - UI label: 13px / DM Sans 500
  - Metadata/caption: 13px / DM Sans 400
  - Badge/tag: 12px / DM Sans 500
  - Code: 14px / JetBrains Mono 400

## Color

### Approach
Restrained with warmth. Amber primary is the signature. Warm gray neutrals reinforce the trades/workshop identity. Semantic colors (success, danger, warning) stay conventional for instant recognition.

### Light Mode

| Token | Hex | Usage |
|-------|-----|-------|
| primary | #B8720E | Buttons, links, active nav, focus rings |
| primary-hover | #9A5F0B | Button hover, link hover |
| primary-light | #FDF3E3 | Selected backgrounds, primary badges |
| background | #F6F5F3 | Page background |
| card | #FEFEFE | Card surfaces, sidebar, modals |
| panel | #F0EEEB | Secondary surfaces, code blocks |
| foreground | #2D2A26 | Primary text |
| muted-foreground | #7A746C | Secondary text, placeholders |
| border | #E3DFD9 | Borders, dividers |
| success | #1B8F46 | Success states |
| success-bg | #E5F5EC | Success backgrounds |
| danger | #C93B37 | Error states, destructive actions |
| danger-bg | #FCE8E8 | Error backgrounds |
| warning | #D4A510 | Warning states |
| warning-bg | #FDF4D6 | Warning backgrounds |
| info | #2E6BB5 | Informational states |
| info-bg | #E3EDF7 | Info backgrounds |
| selected-bg | #FDF3E3 | Active nav items, selected rows |
| secondary-hover | #EDEAE6 | Hover on secondary/ghost elements |

### Primary Scale (Light)
| Step | Hex |
|------|-----|
| 50 | #FDF8F0 |
| 100 | #FDF3E3 |
| 200 | #F5DDB4 |
| 300 | #E8C07A |
| 400 | #D4A030 |
| 500 | #C4860E |
| 600 | #B8720E |
| 700 | #9A5F0B |
| 800 | #7D4D09 |
| 900 | #613C07 |

### Neutral Scale (Light)
| Step | Hex |
|------|-----|
| 50 | #FAF9F7 |
| 100 | #F6F5F3 |
| 200 | #EDEAE6 |
| 300 | #E3DFD9 |
| 400 | #C4BEB5 |
| 500 | #94908A |
| 600 | #7A746C |
| 700 | #5A544D |
| 800 | #3E3A35 |
| 900 | #2D2A26 |

### Dark Mode
Warm charcoal base. Amber brightens slightly for contrast on dark surfaces. Reduce saturation 10-15% on semantic colors.

| Token | Hex |
|-------|-----|
| primary | #D4940F |
| primary-hover | #E5A82A |
| primary-light | #332810 |
| background | #1A1816 |
| card | #262320 |
| panel | #1E1C19 |
| foreground | #E8E4DE |
| muted-foreground | #9A948C |
| border | #3A3630 |
| success | #3CC978 |
| success-bg | #0C3626 |
| danger | #E85450 |
| danger-bg | #351A1A |
| warning | #F0D456 |
| warning-bg | #362008 |
| info | #5498D8 |
| info-bg | #1A2E48 |
| selected-bg | #332810 |
| secondary-hover | #322F2B |

### Dark Mode Primary Scale
| Step | Hex |
|------|-----|
| 50 | #1E1A10 |
| 100 | #332810 |
| 200 | #4A3A18 |
| 300 | #7D4D09 |
| 400 | #B8720E |
| 500 | #D4940F |
| 600 | #E5A82A |

### Dark Mode Neutral Scale
| Step | Hex |
|------|-----|
| 50 | #1A1816 |
| 100 | #262320 |
| 200 | #3A3630 |
| 300 | #524D46 |
| 400 | #6E6860 |
| 500 | #9A948C |
| 600 | #C4BEB5 |

### Dark Mode Shadows
| Token | Value |
|-------|-------|
| shadow-xs | 0 1px 2px rgba(0, 0, 0, 0.2) |
| shadow-sm | 0 2px 4px rgba(0, 0, 0, 0.25), 0 1px 2px rgba(0, 0, 0, 0.2) |
| shadow-md | 0 4px 8px rgba(0, 0, 0, 0.3), 0 2px 4px rgba(0, 0, 0, 0.2) |
| shadow-lg | 0 10px 20px rgba(0, 0, 0, 0.35), 0 4px 8px rgba(0, 0, 0, 0.25) |
| shadow-xl | 0 20px 30px rgba(0, 0, 0, 0.4), 0 8px 12px rgba(0, 0, 0, 0.25) |
| shadow-inner | inset 0 2px 4px rgba(0, 0, 0, 0.15) |

## Spacing
- **Base unit:** 4px
- **Density:** Comfortable (tradespeople on phones, possibly with gloves, in bright sunlight)
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64)

## Layout
- **Approach:** Grid-disciplined
- **Grid:** Sidebar (220-264px fixed) + fluid main content
- **Max content width:** max-w-5xl (64rem)
- **Border radius:**
  - Button: 8px
  - Input: 10px
  - Card: 14px
  - Container/Modal: 18px
  - Full/pill: 9999px

## Motion
- **Approach:** Minimal-functional
- **Easing:** enter(ease-out) exit(ease-in) move(ease-in-out)
- **Duration:** micro(50-100ms) short(150ms) medium(200ms) long(400ms)
- **Animations:**
  - overlay-in: fade 150ms ease-out
  - dialog-in: scale+translate 150ms ease-out
  - message-in: slide-up 8px + fade 200ms ease-out
  - fade-in: opacity 150ms ease-out

## Shadows (Light Mode)
| Token | Value |
|-------|-------|
| shadow-xs | 0 1px 2px rgba(45, 42, 38, 0.04) |
| shadow-sm | 0 1px 3px rgba(45, 42, 38, 0.06), 0 1px 2px rgba(45, 42, 38, 0.04) |
| shadow-md | 0 4px 6px -1px rgba(45, 42, 38, 0.07), 0 2px 4px -2px rgba(45, 42, 38, 0.05) |
| shadow-lg | 0 10px 15px -3px rgba(45, 42, 38, 0.08), 0 4px 6px -4px rgba(45, 42, 38, 0.04) |
| shadow-xl | 0 20px 25px -5px rgba(45, 42, 38, 0.08), 0 8px 10px -6px rgba(45, 42, 38, 0.04) |
| shadow-inner | inset 0 2px 4px rgba(45, 42, 38, 0.04) |

## Visual Effects & Decoration

Clawbolt should match the visual sophistication of octonous.ai (sibling mozilla.ai project). These techniques add depth and professional polish without sacrificing performance.

### Glassmorphism / Frosted Glass
Used for cards and overlays on gradient backgrounds (auth pages, splash, modals over rich backgrounds).

```css
/* Frosted glass card */
bg-card/10 backdrop-blur-xl border border-border/15

/* Modal/overlay on content */
bg-card/80 backdrop-blur-md supports-[backdrop-filter]:bg-card/60
```

- 10% opacity background + `backdrop-blur-xl` (20px) for floating cards on gradient backgrounds
- 80% opacity + `backdrop-blur-md` (12px) for modals over existing content
- Thin border at 15% opacity for subtle framing
- Use `supports-[backdrop-filter]` progressive enhancement

### Gradient Brand Theme (Auth / Splash Pages)
A dedicated theme variant for public-facing pages (login, signup, landing). Dark background with warm amber glow.

```css
/* Background */
background: #2D2A26;  /* warm black base */

/* Gradient overlay */
background: radial-gradient(
  circle at 50% 40%,
  #B8720E 0%,       /* amber center */
  #9A5F0B 15%,      /* deep amber */
  #7D4D09 30%,      /* dark amber */
  #5A4430 50%,      /* warm brown */
  #3E3A35 70%,      /* charcoal */
  #2D2A26 100%      /* warm black */
);
mix-blend-mode: screen;
opacity: 0.6;
```

- Dark warm base with radial amber glow
- `mix-blend-mode: screen` for natural light blending
- Cards on this background use frosted glass treatment
- Content surfaces should be light (white/off-white) for contrast with dark backdrop

### Subtle Grain Texture
A fine noise overlay that adds tactile quality to backgrounds. Evokes workshop/craft materials.

```css
/* Apply to page backgrounds or sidebar */
background-image: url("data:image/svg+xml,..."); /* inline noise SVG or tiny PNG */
opacity: 0.03;  /* light mode: barely visible */
opacity: 0.05;  /* dark mode: slightly more visible */
```

- Use sparingly: page background and sidebar only, not on every card
- Opacity kept very low (3-5%) for subtlety
- Adds warmth and tactile quality without visual noise

### Decorative Background Patterns
Optional: subtle geometric or tool-inspired patterns at very low opacity for brand personality on marketing/splash pages. Not for the dashboard app shell.

### Where to Use Each Effect

| Context | Treatment |
|---------|-----------|
| Auth pages (login, signup) | Gradient brand theme + frosted glass cards |
| Landing/splash page | Gradient brand theme + frosted glass feature cards |
| Dashboard app shell | Standard light/dark theme, subtle grain on background |
| Modals/overlays | Backdrop blur at 80% opacity |
| Search overlay | Backdrop blur (medium) with progressive enhancement |
| Cards in app | Standard solid background with shadow, no blur |

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-20 | Initial design system: Amber Workshop | Created by /design-consultation. Competitive research across Housecall Pro, ServiceTitan, ContractorPlus showed convergence on dark themes + warm accents. Amber primary differentiates from blue-dominated space while mapping to trades visual language (hard hats, tools, construction). Warm neutrals reinforce identity. Outfit + DM Sans replace Inter for personality. Text sizes bumped for field use. |
| 2026-03-20 | Added visual effects: glassmorphism, gradients, grain | Inspired by octonous.ai (sibling mozilla.ai project). Frosted glass cards for auth/splash pages, gradient brand theme with warm amber glow, subtle grain texture for depth. Decoration level upgraded from "intentional" to "expressive." |
