// Filter control bar for activity-style log views.
//
// The pattern: date-range pickers, type-toggle pills, "errors only"
// checkbox, free-text search, and a result-count tally. Originally lived
// inline in the Shared tab's per-user activity timeline. Hoisted here
// because the same control surface drives the global Activity feed
// (multi-user) and is a candidate for the Reported queue's filter row.
//
// Generic over the activity type union so consumers can pin their own
// closed set of types (`'conversation' | 'heartbeat' | 'compaction'`,
// or whatever they merge). The labels and pill colors are passed in so
// the component stays presentation-only.

import { type ReactNode } from 'react';

export interface ActivityFilterBarProps<T extends string> {
  startDate: string;
  endDate: string;
  onStartDateChange: (v: string) => void;
  onEndDateChange: (v: string) => void;
  /** Closed set of types the consumer wants to surface in the toggle row. */
  types: readonly T[];
  /** Display labels and pill classes for each type. */
  typeLabels: Record<T, string>;
  enabledTypes: Set<T>;
  onToggleType: (t: T) => void;
  errorsOnly: boolean;
  onErrorsOnlyChange: (v: boolean) => void;
  search: string;
  onSearchChange: (v: string) => void;
  resultCount: number;
  totalCount: number;
  /** Optional slot for extra controls (e.g. a per-user filter on the global feed). */
  extraControls?: ReactNode;
  /** When provided, render a refresh button that re-runs the data fetches. */
  onRefresh?: () => void;
  /** Disable the refresh button while a fetch is in flight. */
  isRefreshing?: boolean;
  /**
   * Current feed sort direction. When paired with `onToggleSortDirection`,
   * renders a toggle button that flips newest-first / oldest-first. Omit both
   * for consumers that don't expose a sort control.
   */
  sortDirection?: 'newest' | 'oldest';
  onToggleSortDirection?: () => void;
}

export default function ActivityFilterBar<T extends string>({
  startDate,
  endDate,
  onStartDateChange,
  onEndDateChange,
  types,
  typeLabels,
  enabledTypes,
  onToggleType,
  errorsOnly,
  onErrorsOnlyChange,
  search,
  onSearchChange,
  resultCount,
  totalCount,
  extraControls,
  onRefresh,
  isRefreshing,
  sortDirection,
  onToggleSortDirection,
}: ActivityFilterBarProps<T>) {
  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3 mb-3 space-y-2">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs flex flex-col">
          <span className="text-muted-foreground mb-0.5">Start</span>
          <input
            type="date"
            value={startDate}
            onChange={e => onStartDateChange(e.target.value)}
            className="border border-border rounded-[--radius-sm] px-2 py-1 text-xs bg-background"
          />
        </label>
        <label className="text-xs flex flex-col">
          <span className="text-muted-foreground mb-0.5">End</span>
          <input
            type="date"
            value={endDate}
            onChange={e => onEndDateChange(e.target.value)}
            className="border border-border rounded-[--radius-sm] px-2 py-1 text-xs bg-background"
          />
        </label>
        {(startDate || endDate) && (
          <button
            type="button"
            onClick={() => {
              onStartDateChange('');
              onEndDateChange('');
            }}
            className="text-xs text-primary hover:underline pb-1"
          >
            Clear range
          </button>
        )}
        <label className="text-xs flex-1 min-w-[180px] flex flex-col">
          <span className="text-muted-foreground mb-0.5">Search</span>
          <input
            type="text"
            value={search}
            onChange={e => onSearchChange(e.target.value)}
            placeholder="Search messages, tool calls, args, results"
            className="border border-border rounded-[--radius-sm] px-2 py-1 text-xs bg-background"
          />
        </label>
        {extraControls}
        {sortDirection && onToggleSortDirection && (
          <button
            type="button"
            onClick={onToggleSortDirection}
            title="Toggle sort order"
            className="border border-border rounded-[--radius-sm] px-2 py-1 text-xs bg-background hover:bg-secondary-hover"
          >
            {/* Arrow is decorative; the text is the button's accessible name
                so it matches the visible label (WCAG 2.5.3 Label in Name). */}
            <span aria-hidden="true">
              {sortDirection === 'newest' ? '↓' : '↑'}
            </span>{' '}
            {sortDirection === 'newest' ? 'Newest first' : 'Oldest first'}
          </button>
        )}
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            disabled={isRefreshing}
            aria-label="Refresh activity"
            className="border border-border rounded-[--radius-sm] px-2 py-1 text-xs bg-background hover:bg-secondary-hover disabled:opacity-50"
          >
            {isRefreshing ? 'Refreshing...' : '↻ Refresh'}
          </button>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">Types:</span>
        {types.map(t => {
          const on = enabledTypes.has(t);
          return (
            <button
              key={t}
              type="button"
              onClick={() => onToggleType(t)}
              aria-pressed={on}
              className={`px-2 py-0.5 rounded-[--radius-full] border ${
                on
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border text-muted-foreground'
              }`}
            >
              {typeLabels[t]}
            </button>
          );
        })}
        <label className="ml-2 inline-flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={errorsOnly}
            onChange={e => onErrorsOnlyChange(e.target.checked)}
          />
          <span>Errors only</span>
        </label>
        <span className="ml-auto text-muted-foreground">
          {resultCount} / {totalCount}
        </span>
      </div>
    </div>
  );
}
