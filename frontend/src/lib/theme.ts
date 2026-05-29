/**
 * Color-theme preference: 'system' (follow OS), 'light', or 'dark'.
 *
 * The effective theme is applied by toggling the `dark` class on <html>, which
 * activates the dark design tokens in styles/brand-tokens.css. A pre-paint
 * inline script in index.html applies the stored preference before React
 * mounts (no flash); this module keeps it in sync at runtime. Keep the storage
 * key and the dark-resolution logic identical to that inline script.
 */
export type ThemePreference = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'clawbolt-theme';

export function getStoredTheme(): ThemePreference {
  const v = localStorage.getItem(STORAGE_KEY);
  return v === 'light' || v === 'dark' ? v : 'system';
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

export function resolveDark(pref: ThemePreference): boolean {
  return pref === 'dark' || (pref === 'system' && systemPrefersDark());
}

export function applyTheme(pref: ThemePreference): void {
  const dark = resolveDark(pref);
  document.documentElement.classList.toggle('dark', dark);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#1A1816' : '#F6F5F3');
}

export function setStoredTheme(pref: ThemePreference): void {
  if (pref === 'system') localStorage.removeItem(STORAGE_KEY);
  else localStorage.setItem(STORAGE_KEY, pref);
  applyTheme(pref);
}

/** Subscribe to OS theme changes; only re-applies while preference is 'system'. */
export function watchSystemTheme(getPref: () => ThemePreference): () => void {
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
  if (!mq) return () => {};
  const handler = (): void => {
    if (getPref() === 'system') applyTheme('system');
  };
  mq.addEventListener('change', handler);
  return () => mq.removeEventListener('change', handler);
}
