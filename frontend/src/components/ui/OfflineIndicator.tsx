import { useState, useEffect } from 'react';

/**
 * Banner displayed at the top of the viewport when the browser is offline.
 * Listens to the native online/offline events and auto-dismisses when
 * connectivity returns.
 */
export default function OfflineIndicator() {
  const [isOffline, setIsOffline] = useState(!navigator.onLine);

  useEffect(() => {
    const goOffline = () => setIsOffline(true);
    const goOnline = () => setIsOffline(false);

    window.addEventListener('offline', goOffline);
    window.addEventListener('online', goOnline);

    return () => {
      window.removeEventListener('offline', goOffline);
      window.removeEventListener('online', goOnline);
    };
  }, []);

  if (!isOffline) return null;

  return (
    <div
      role="status"
      className="fixed top-0 inset-x-0 z-[100] flex items-center justify-center gap-2 bg-warning/90 text-warning-foreground px-4 py-2 text-sm font-medium shadow-sm"
    >
      <svg
        className="w-4 h-4 shrink-0"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M18.364 5.636a9 9 0 010 12.728M5.636 18.364a9 9 0 010-12.728M8.464 15.536a5 5 0 010-7.072M15.536 8.464a5 5 0 010 7.072M12 12h.01"
        />
        <line x1="4" y1="4" x2="20" y2="20" strokeWidth={2} strokeLinecap="round" />
      </svg>
      You are offline. Some features may be unavailable.
    </div>
  );
}
