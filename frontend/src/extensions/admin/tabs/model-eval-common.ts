import type { EvalRecommendation } from '../admin-api';

// Shared by the model-eval index page (the run form and the run history) and
// the report page each run links to. Both poll, both decide whether a run is
// still in flight, and both render the verdict pill, so these three live here
// rather than being duplicated or imported across two page modules.

// Poll cadence while a run is in flight. A hundred-turn run takes minutes and
// writes a row per turn, so this is fast enough to look alive and slow enough
// not to hammer the endpoint.
export const POLL_MS = 2000;

export const ACTIVE_STATUSES = new Set(['pending', 'running']);

export const RECOMMENDATION_COPY: Record<
  EvalRecommendation,
  { label: string; className: string }
> = {
  safe_to_switch: {
    label: 'Safe to switch',
    className: 'bg-success-bg text-success-text border-success/30',
  },
  switch_with_monitoring: {
    label: 'Switch with monitoring',
    className: 'bg-warning-bg text-warning-text border-warning/30',
  },
  do_not_switch: {
    label: 'Do not switch',
    className: 'bg-error-bg text-error-text border-danger/30',
  },
  inconclusive: {
    label: 'Inconclusive',
    className: 'bg-panel text-muted-foreground border-border',
  },
};
