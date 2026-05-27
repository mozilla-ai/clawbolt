import { addToast } from '@heroui/toast';

const DURATIONS = {
  success: 4000,
  danger: 8000,
} as const;

type ToastColor = keyof typeof DURATIONS;

/** Keys of toasts currently on screen, used to suppress duplicates. */
const activeToasts = new Map<string, ReturnType<typeof setTimeout>>();

function dedupKey(title: string, color: ToastColor): string {
  return `${color}:${title}`;
}

// HeroUI's default toast styles apply `truncate` to the title slot, which
// silently clips longer error messages with no way to expand. Override the
// title slot so the text wraps onto multiple lines instead.
const TITLE_CLASS_NAMES = { title: 'whitespace-normal break-words' } as const;

function showToast(title: string, color: ToastColor): void {
  const key = dedupKey(title, color);
  if (activeToasts.has(key)) return;
  const duration = DURATIONS[color];
  addToast({ title, color, timeout: duration, classNames: TITLE_CLASS_NAMES });
  const timer = setTimeout(() => {
    activeToasts.delete(key);
  }, duration);
  activeToasts.set(key, timer);
}

export const toast = {
  success: (title: string) => showToast(title, 'success'),
  error: (title: string) => showToast(title, 'danger'),
};

/** Reset internal state. Exported only for tests. */
export function _resetActiveToasts(): void {
  for (const timer of activeToasts.values()) {
    clearTimeout(timer);
  }
  activeToasts.clear();
}
