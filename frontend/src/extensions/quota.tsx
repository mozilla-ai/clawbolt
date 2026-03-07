/** OSS stub: premium overlay replaces this with real quota UI. */
import type { ReactNode } from 'react';

export function QuotaBanner(): null {
  return null;
}

export function OnboardingBanner({ children }: { children?: ReactNode }): ReactNode {
  return children ?? null;
}

export function isQuotaError(_message: string): boolean {
  return false;
}
