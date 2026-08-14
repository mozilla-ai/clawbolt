import type { ReactNode } from 'react';

/**
 * Premium override of the OSS header-badge hook plus the shared BetaBadge
 * component used by MarketingLayout, DocsLayout, and LoginPage to tag the
 * hosted clawbolt.ai deployment.
 *
 * Requires OSS extension hook: renderHeaderBadge (mozilla-ai/clawbolt#1395).
 */
export function renderHeaderBadge(): ReactNode {
  return <BetaBadge variant="light" />;
}

type BetaBadgeVariant = 'light' | 'dark';

const VARIANT_CLASSES: Record<BetaBadgeVariant, string> = {
  light: 'bg-primary/15 text-primary border-primary/25',
  dark: 'bg-white/15 text-white border-white/20',
};

export function BetaBadge({ variant = 'light' }: { variant?: BetaBadgeVariant }) {
  return (
    <span
      aria-label="Beta release"
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide border ${VARIANT_CLASSES[variant]}`}
    >
      Beta
    </span>
  );
}
