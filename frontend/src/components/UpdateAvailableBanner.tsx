import { useRegisterSW } from 'virtual:pwa-register/react';

/**
 * Polls the SW for a new build on a fixed interval. Browsers only auto-check
 * for SW updates on navigation, which rarely happens in a SPA / installed PWA,
 * so without this users would not see a fresh build until they reopen the app.
 */
const UPDATE_POLL_INTERVAL_MS = 60 * 60 * 1000;

export default function UpdateAvailableBanner() {
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    updateServiceWorker,
  } = useRegisterSW({
    onRegisteredSW(_swUrl, registration) {
      if (!registration) return;
      const tick = () => {
        if (registration.installing || !navigator.onLine) return;
        void registration.update();
      };
      setInterval(tick, UPDATE_POLL_INTERVAL_MS);
    },
  });

  if (!needRefresh) return null;

  return (
    <div
      role="alert"
      aria-live="polite"
      className="fixed top-0 inset-x-0 z-[100] flex items-center justify-center gap-3 bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm"
    >
      <span>A new version of Clawbolt is available.</span>
      <button
        type="button"
        onClick={() => {
          void updateServiceWorker(true);
        }}
        className="rounded-md bg-card px-3 py-1 text-xs font-semibold text-primary shadow-sm transition-colors can-hover:hover:bg-card/90"
      >
        Update
      </button>
      <button
        type="button"
        onClick={() => setNeedRefresh(false)}
        aria-label="Dismiss update notification"
        className="rounded-md px-2 py-1 text-xs font-medium text-primary-foreground/80 transition-colors can-hover:hover:text-primary-foreground"
      >
        Later
      </button>
    </div>
  );
}
