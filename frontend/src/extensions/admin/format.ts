/** Small formatting helpers used across admin tabs. */

/** Returns "just now" / "3 min ago" / "2 days ago" / a date. Empty string for empty input. */
export function formatRelative(ts: string | null | undefined): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.round(diffMs / 1000);
  if (diffSec < 30) return 'just now';
  if (diffSec < 90) return '1 min ago';
  if (diffSec < 3_600) return `${Math.round(diffSec / 60)} min ago`;
  if (diffSec < 90_000) {
    const hr = Math.round(diffSec / 3_600);
    return `${hr} hr${hr === 1 ? '' : 's'} ago`;
  }
  if (diffSec < 2_592_000) {
    const days = Math.round(diffSec / 86_400);
    return `${days} day${days === 1 ? '' : 's'} ago`;
  }
  if (diffSec < 31_536_000) {
    const months = Math.round(diffSec / 2_592_000);
    return `${months} month${months === 1 ? '' : 's'} ago`;
  }
  return d.toLocaleDateString();
}

/** Absolute date+time for use in `title` attributes. */
export function formatAbsolute(ts: string | null | undefined): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

/** Absolute local date+time as `YYYY-MM-DD HH:MM:SS`. */
export function formatAbsoluteWithSeconds(ts: string | null | undefined): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const pad = (n: number): string => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

/** Best-effort plan color map. Free = muted, Pro = amber, anything else = info. */
export function planPillClass(plan: string): string {
  const p = plan.toLowerCase();
  if (p === 'free') return 'bg-panel text-muted-foreground';
  if (p === 'pro' || p === 'paid' || p === 'premium')
    return 'bg-primary-light text-primary';
  return 'bg-info-bg text-info';
}
