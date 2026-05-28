import type { ReactNode } from 'react';

/**
 * Extension hook for rendering a small badge next to the Clawbolt logo
 * (sidebar logo + mobile header). OSS returns null; premium overrides to
 * tag the hosted deployment with status like "Beta".
 */
export function renderHeaderBadge(): ReactNode {
  return null;
}
