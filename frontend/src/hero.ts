import { heroui } from '@heroui/theme/plugin';
import { lightColors, darkColors } from './styles/palette';

/*
 * HeroUI plugin config. Color values come from ./styles/palette.ts (the single
 * source of truth). The plugin compiles these into `--heroui-*` HSL-channel CSS
 * variables at build time; ./styles/heroui-tokens.generated.css then rebinds
 * those variables to `var(--brand-h-*)` tokens so HeroUI components follow our
 * design tokens at runtime. Layout tokens (radius) mirror DESIGN.md.
 */
export default heroui({
  prefix: 'heroui',
  addCommonColors: false,
  defaultTheme: 'light',
  defaultExtendTheme: 'light',
  layout: {
    radius: {
      small: '8px',
      medium: '10px',
      large: '14px',
    },
    borderWidth: {
      small: '1px',
      medium: '1px',
      large: '2px',
    },
    disabledOpacity: '0.5',
  },
  themes: {
    light: {
      colors: { ...lightColors },
    },
    dark: {
      extend: 'dark',
      colors: { ...darkColors },
    },
  },
});
