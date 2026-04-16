/**
 * SVG icons for integrations displayed on the Tools page.
 *
 * Each icon is a 20x20 inline SVG. Brand colors are used where appropriate.
 * Generic icons are provided for tools without a specific brand mark.
 */

function QuickBooksIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" fill="none">
      <rect width="24" height="24" rx="4" fill="#2CA01C" />
      <path
        d="M7.5 7C6.12 7 5 8.12 5 9.5v5C5 15.88 6.12 17 7.5 17H9v-1.5H7.5c-.55 0-1-.45-1-1v-5c0-.55.45-1 1-1H9V10h2V7H7.5zM16.5 7H15v1.5h1.5c.55 0 1 .45 1 1v5c0 .55-.45 1-1 1H15V14h-2v3h3.5c1.38 0 2.5-1.12 2.5-2.5v-5C19 8.12 17.88 7 16.5 7z"
        fill="white"
      />
    </svg>
  );
}

function GoogleCalendarIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" fill="none">
      <rect width="24" height="24" rx="4" fill="#4285F4" />
      <rect x="6" y="4" width="12" height="16" rx="1.5" fill="white" />
      <rect x="6" y="4" width="12" height="4" rx="1.5" fill="#EA4335" />
      <rect x="8" y="10" width="2.5" height="2" rx="0.3" fill="#4285F4" />
      <rect x="11.5" y="10" width="2.5" height="2" rx="0.3" fill="#4285F4" />
      <rect x="8" y="14" width="2.5" height="2" rx="0.3" fill="#4285F4" />
      <rect x="11.5" y="14" width="2.5" height="2" rx="0.3" fill="#4285F4" />
      <rect x="15" y="10" width="1.5" height="2" rx="0.3" fill="#4285F4" opacity="0.5" />
    </svg>
  );
}

function CompanyCamIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" fill="none">
      <rect width="24" height="24" rx="4" fill="#FF6B35" />
      <path
        d="M7 9.5C7 8.67 7.67 8 8.5 8h7c.83 0 1.5.67 1.5 1.5v5c0 .83-.67 1.5-1.5 1.5h-7C7.67 16 7 15.33 7 14.5v-5z"
        fill="white"
      />
      <circle cx="12" cy="12" r="2.2" fill="#FF6B35" />
      <circle cx="12" cy="12" r="1.3" fill="white" />
      <circle cx="15" cy="9.5" r="0.6" fill="#FF6B35" />
    </svg>
  );
}

function PricingIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" fill="none">
      <rect width="24" height="24" rx="4" fill="#F96302" />
      <path
        d="M12 6v1.5m0 9V18m3.5-6.75c0-1.24-1.57-2.25-3.5-2.25s-3.5 1.01-3.5 2.25S10.07 13.5 12 13.5s3.5 1.01 3.5 2.25S13.93 18 12 18"
        stroke="white"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function DefaultIntegrationIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" fill="none">
      <rect width="24" height="24" rx="4" fill="currentColor" className="text-default-300" />
      <path
        d="M12 6v2m0 8v2M6 12h2m8 0h2m-1.5-4.5L15 9m-6 6-1.5 1.5M17.5 17.5 16 16M7.5 7.5 9 9"
        stroke="white"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

import type { ReactNode } from 'react';

const ICON_MAP: Record<string, () => ReactNode> = {
  quickbooks: QuickBooksIcon,
  calendar: GoogleCalendarIcon,
  companycam: CompanyCamIcon,
  supplier_pricing: PricingIcon,
};

export function IntegrationIcon({ name }: { name: string }) {
  const Icon = ICON_MAP[name] ?? DefaultIntegrationIcon;
  return <Icon />;
}
