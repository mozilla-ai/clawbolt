import { useCallback, useEffect, useState } from 'react';
import {
  type ThemePreference,
  getStoredTheme,
  setStoredTheme,
  watchSystemTheme,
} from '@/lib/theme';

/**
 * Read and set the color-theme preference. The effective theme is applied to
 * <html> immediately (and pre-paint by index.html); this hook keeps React state
 * in sync and re-applies on OS changes while the preference is 'system'.
 */
export function useTheme(): { theme: ThemePreference; setTheme: (t: ThemePreference) => void } {
  const [theme, setThemeState] = useState<ThemePreference>(getStoredTheme);

  useEffect(() => watchSystemTheme(() => theme), [theme]);

  const setTheme = useCallback((t: ThemePreference) => {
    setStoredTheme(t);
    setThemeState(t);
  }, []);

  return { theme, setTheme };
}
