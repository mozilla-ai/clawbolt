import { useTheme } from '@/hooks/useTheme';
import type { ThemePreference } from '@/lib/theme';
import type { ReactNode } from 'react';

/** Segmented System / Light / Dark control. Token-styled; no hard-coded colors. */
export default function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const options: { value: ThemePreference; label: string; icon: ReactNode }[] = [
    { value: 'system', label: 'System theme', icon: <SystemIcon /> },
    { value: 'light', label: 'Light theme', icon: <SunIcon /> },
    { value: 'dark', label: 'Dark theme', icon: <MoonIcon /> },
  ];
  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-md bg-panel p-0.5"
      role="radiogroup"
      aria-label="Color theme"
    >
      {options.map((opt) => {
        const active = theme === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={opt.label}
            title={opt.label}
            onClick={() => setTheme(opt.value)}
            className={`flex items-center justify-center size-7 rounded transition-colors ${
              active
                ? 'bg-card text-foreground shadow-xs'
                : 'text-muted-foreground can-hover:hover:text-foreground'
            }`}
          >
            {opt.icon}
          </button>
        );
      })}
    </div>
  );
}

function SystemIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <rect x="3" y="4" width="18" height="12" rx="1.5" strokeWidth={1.5} />
      <path strokeLinecap="round" strokeWidth={1.5} d="M8 20h8M12 16v4" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="4" strokeWidth={1.5} />
      <path strokeLinecap="round" strokeWidth={1.5} d="M12 2v2m0 16v2M4 12H2m20 0h-2M5.6 5.6 4.2 4.2m15.6 15.6-1.4-1.4M5.6 18.4 4.2 19.8M19.8 4.2l-1.4 1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}
