import { useEffect, useState } from 'react';

const MOBILE_BREAKPOINT = '(max-width: 640px)';

/** True when the viewport matches the mobile breakpoint. SSR-safe: returns
 * false during the first render and updates after mount. */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia(MOBILE_BREAKPOINT);
    setIsMobile(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  return isMobile;
}
