import { useRef, useEffect, useCallback } from 'react';

/** Minimum horizontal distance (px) for a swipe to register. */
const SWIPE_THRESHOLD = 50;

/** Maximum width (px) from the left edge where a swipe-open gesture can start. */
const EDGE_ZONE = 30;

/** Maximum vertical drift (px) before the gesture is rejected (prevents scroll hijacking). */
const MAX_VERTICAL_DRIFT = 75;

/** Breakpoint (px) above which swipe is disabled (matches Tailwind md:). */
const MD_BREAKPOINT = 768;

interface UseSwipeSidebarOptions {
  /** Whether the sidebar is currently open. */
  isOpen: boolean;
  /** Callback to open the sidebar. */
  onOpen: () => void;
  /** Callback to close the sidebar. */
  onClose: () => void;
}

/**
 * Adds touch-swipe gestures for the mobile sidebar.
 *
 * - Swipe right from the left edge of the screen to open.
 * - Swipe left anywhere when the sidebar is open to close.
 * - Disabled on screens wider than `md` (768px).
 */
export default function useSwipeSidebar({ isOpen, onOpen, onClose }: UseSwipeSidebarOptions) {
  const touchStart = useRef<{ x: number; y: number } | null>(null);
  const startedInEdge = useRef(false);

  const handleTouchStart = useCallback(
    (e: TouchEvent) => {
      if (window.innerWidth >= MD_BREAKPOINT) return;

      const touch = e.touches[0];
      if (!touch) return;
      touchStart.current = { x: touch.clientX, y: touch.clientY };
      startedInEdge.current = touch.clientX <= EDGE_ZONE;
    },
    [],
  );

  const handleTouchEnd = useCallback(
    (e: TouchEvent) => {
      if (!touchStart.current) return;
      if (window.innerWidth >= MD_BREAKPOINT) return;

      const touch = e.changedTouches[0];
      if (!touch) return;
      const dx = touch.clientX - touchStart.current.x;
      const dy = Math.abs(touch.clientY - touchStart.current.y);

      // Reject if vertical drift is too large (user is scrolling, not swiping)
      if (dy > MAX_VERTICAL_DRIFT) {
        touchStart.current = null;
        return;
      }

      if (!isOpen && startedInEdge.current && dx > SWIPE_THRESHOLD) {
        onOpen();
      } else if (isOpen && dx < -SWIPE_THRESHOLD) {
        onClose();
      }

      touchStart.current = null;
    },
    [isOpen, onOpen, onClose],
  );

  useEffect(() => {
    document.addEventListener('touchstart', handleTouchStart, { passive: true });
    document.addEventListener('touchend', handleTouchEnd, { passive: true });

    return () => {
      document.removeEventListener('touchstart', handleTouchStart);
      document.removeEventListener('touchend', handleTouchEnd);
    };
  }, [handleTouchStart, handleTouchEnd]);
}
