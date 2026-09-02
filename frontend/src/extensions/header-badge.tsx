import type { ReactNode } from 'react';

/**
 * Implementation of the OSS ``renderHeaderBadge`` extension hook
 * (mozilla-ai/clawbolt#1395), called by ``AppShell``.
 *
 * clawbolt.ai is out of beta, so it badges nothing. The hook stays because it
 * is the seam a deployment uses to tag its own header, and the next thing
 * worth saying there ("Preview", "Maintenance") is one return statement away.
 * Returning null here is what "no badge" looks like; AppShell renders whatever
 * this gives it.
 */
export function renderHeaderBadge(): ReactNode {
  return null;
}
