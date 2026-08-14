// Visual indicator for users who have opted into research data sharing.
//
// Surfaces the same way across all admin tabs that touch user identity
// (Users list, User-detail header, Reported queue rows, Shared queue
// rows). Centralizing it here keeps the badge styling and tooltip
// consistent so admins build one mental model: green shield = consenting,
// no badge = no consent.
//
// `consentAt` is the user's opt-in timestamp. It feeds the tooltip so an
// admin hovering the badge sees when consent was granted without
// drilling into the user. Pass null/undefined to render nothing.

import { formatAbsolute, formatRelative } from '../format';

interface ConsentBadgeProps {
  consentAt: string | null | undefined;
  /** When true, render only the icon (compact placement: tables, breadcrumbs). */
  compact?: boolean;
  /** Override the tooltip. Default: "Shared data: opted in {relative}". */
  title?: string;
}

export default function ConsentBadge({ consentAt, compact = false, title }: ConsentBadgeProps) {
  if (!consentAt) return null;

  const tooltip = title || `Shared data: opted in ${formatRelative(consentAt)} (${formatAbsolute(consentAt)})`;

  return (
    <span
      className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-[--radius-full] bg-success-bg text-success font-medium"
      title={tooltip}
      aria-label={tooltip}
    >
      <svg
        className="w-3 h-3"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.6-2.6a9 9 0 11-12.7 12.7 9 9 0 0112.7-12.7z" />
      </svg>
      {!compact && 'shared'}
    </span>
  );
}
