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

function showToast(title: string, color: ToastColor): void {
  const key = dedupKey(title, color);
  if (activeToasts.has(key)) return;
  const duration = DURATIONS[color];
  addToast({ title, color, timeout: duration });
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
