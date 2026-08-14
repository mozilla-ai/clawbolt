import { useState, useEffect, useCallback } from 'react';
import {
  getMonitoringStatus,
  runHealthProbes,
  sendMonitoringTestAlert,
  diagnoseEmailDelivery,
  type MonitoringStatus,
  type MonitoringProbe,
  type MonitoringEvent,
  type MonitoringRun,
  type MonitoringRunStep,
  type MonitoringEmail,
  type EmailDiagnostics,
  type RunStepStatus,
} from '../admin-api';
import { formatRelative, formatAbsolute } from '../format';

// Monitoring tab: every dependency probe in one place, not just the one that
// prompted the question. The probes run in-app on a timer and email on status
// transitions; this view answers "is it working right now" and "what changed"
// without waiting for the next email.
//
// Probe keys are grouped: per-tenant keys carry ``user_id`` and can number in
// the hundreds, so they collapse into one row per user and only users with
// something wrong are expanded by default. Infrastructure probes always show.
//
// The per-user section groups by user rather than listing every
// (user, integration) pair flat. A flat list answers "is anything broken"; the
// question an admin actually has is "whose account is broken", and a 200-row
// list sorted by probe key cannot answer it without scrolling and mental
// grouping. Each user gets one verdict line: failing beats unknown beats
// working, and integrations nobody connected are excluded from the verdict.
//
// Two things are deliberately placed rather than merely present:
//
// - ``Run probes now`` is the only action at the top, and a run reports itself
//   step by step. The run is started server-side and polled, because a full
//   pass can take minutes and awaiting it left this tab on "Running" with
//   nothing to show and no way to tell a slow probe from a wedged one.
// - Email delivery lives at the bottom, with ``Send test alert`` as a quiet
//   action inside it. Sending a test is a rare, deliberate act next to the
//   diagnostics that explain a failure; giving it equal billing at the top
//   invited clicking it as the first move on any question.

const INTEGRATION_PREFIX = 'integration:';
// The per-user sweep's own result: could this user be checked at all. Shares
// the section but not the ``integration:`` prefix, so both are matched on
// ``user_id`` rather than on the key.
const INTEGRATION_CHECK_PREFIX = 'integration_check:';

// While a run is in flight the tab re-reads status on this cadence. Fast enough
// that steps visibly advance, slow enough that a 20-probe deployment is not
// re-serialized every frame.
const RUN_POLL_MS = 1500;

type StatusKind = MonitoringEvent['status'];

function statusPillClass(status: StatusKind): string {
  if (status === 'up') return 'bg-success-bg text-success-text';
  if (status === 'down') return 'bg-error-bg text-error-text';
  if (status === 'repaired') return 'bg-info-bg text-info-text';
  return 'bg-panel text-muted-foreground';
}

function statusDotClass(status: StatusKind): string {
  if (status === 'up') return 'bg-success';
  if (status === 'down') return 'bg-danger';
  if (status === 'repaired') return 'bg-info';
  return 'bg-muted-foreground/40';
}

function statusLabel(status: StatusKind): string {
  if (status === 'up') return 'Up';
  if (status === 'down') return 'Down';
  if (status === 'repaired') return 'Repaired';
  return 'Not yet checked';
}

const PILL_CLASS =
  'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[--radius-full] text-[11px] font-semibold whitespace-nowrap';

function StatusPill({ status }: { status: StatusKind }) {
  return (
    <span className={`${PILL_CLASS} ${statusPillClass(status)}`}>
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${statusDotClass(status)}`} aria-hidden />
      {statusLabel(status)}
    </span>
  );
}

function ProbeRow({
  probeKey,
  probe,
  threshold,
  title,
}: {
  probeKey: string;
  probe: MonitoringProbe;
  threshold: number;
  /** Overrides ``probe.label``. Inside a user's group the label repeats the
   *  group heading ("quickbooks for alice@example.com"), so the row says only
   *  what the group heading does not. */
  title?: string;
}) {
  // A probe that has failed, but not yet ``threshold`` times in a row, is still
  // UNKNOWN in the backend's state machine: DOWN is deliberately withheld so one
  // timed-out request to a residential host is not an outage. Rendering that as
  // "Not yet checked" next to a failure detail reads as a bug in the panel, so
  // the pill says what is actually happening and how close it is to DOWN.
  const failingBelowThreshold = probe.status === 'unknown' && probe.consecutive_failures > 0;
  return (
    <div className="flex flex-col gap-1 py-3 border-b border-border last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium truncate">{title ?? probe.label}</p>
          <p className="text-[11px] text-muted-foreground font-mono truncate">{probeKey}</p>
        </div>
        {probe.never_connected ? (
          <span className={`${PILL_CLASS} bg-panel text-muted-foreground`}>
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-muted-foreground/40" aria-hidden />
            Not connected
          </span>
        ) : failingBelowThreshold ? (
          <span className={`${PILL_CLASS} bg-warning-bg text-warning-text`}>
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-warning" aria-hidden />
            Failing {probe.consecutive_failures} of {threshold}
          </span>
        ) : (
          <StatusPill status={probe.status} />
        )}
      </div>
      {probe.detail && (
        <p className="text-xs text-muted-foreground break-words">{probe.detail}</p>
      )}
      <p className="text-[11px] text-muted-foreground">
        {probe.status === 'down' && probe.consecutive_failures > 0 && (
          <>
            {probe.consecutive_failures} consecutive failure
            {probe.consecutive_failures === 1 ? '' : 's'} &middot;{' '}
          </>
        )}
        <span title={formatAbsolute(probe.since)}>since {formatRelative(probe.since)}</span>
        {probe.last_checked && (
          <span title={formatAbsolute(probe.last_checked)}>
            {' '}
            &middot; checked {formatRelative(probe.last_checked)}
          </span>
        )}
      </p>
    </div>
  );
}

// --- Per-user integration grouping -----------------------------------------
//
// Four outcomes, ordered worst first. ``unknown`` is its own outcome rather
// than being folded into either neighbour: it means the check could not answer
// (a timed-out sweep, or a failure that has not yet reached the DOWN
// threshold), and treating that as healthy is how a real breakage hides.

type IntegrationHealth = 'failing' | 'unknown' | 'working' | 'notConnected';

// Worst first. A group's verdict is the worst outcome among its integrations,
// with one override: if the sweep could not check this user at all, nothing
// below "unknown" can be claimed about them.
const VERDICT_ORDER: IntegrationHealth[] = ['failing', 'unknown', 'working', 'notConnected'];

function classifyProbe(probe: MonitoringProbe): IntegrationHealth {
  // Checked before ``down`` on purpose: never-connected rows are stored as DOWN
  // so a later disconnect is still reportable, but they are a user's choice.
  if (probe.never_connected) return 'notConnected';
  if (probe.status === 'down') return 'failing';
  if (probe.status === 'up') return 'working';
  // Includes a failure that has not yet reached the DOWN threshold. Reporting
  // that as healthy is how a real breakage hides for a tick.
  return 'unknown';
}

interface UserGroup {
  userId: string;
  label: string;
  rows: [string, MonitoringProbe][];
  counts: Record<IntegrationHealth, number>;
  /** True when the per-user sweep check itself is failing. */
  uncheckable: boolean;
  worst: IntegrationHealth;
}

/** One row per user, worst first, so the accounts needing attention lead. */
function groupByUser(probes: [string, MonitoringProbe][]): UserGroup[] {
  const groups = new Map<string, UserGroup>();
  for (const entry of probes) {
    const [key, probe] = entry;
    const userId = probe.user_id || key;
    let group = groups.get(userId);
    if (!group) {
      group = {
        userId,
        label: probe.user_label || userId,
        rows: [],
        counts: { failing: 0, unknown: 0, working: 0, notConnected: 0 },
        uncheckable: false,
        worst: 'notConnected',
      };
      groups.set(userId, group);
    }
    group.rows.push(entry);
    if (key.startsWith(INTEGRATION_CHECK_PREFIX)) {
      // Not one of the user's integrations, so it is not counted as one. It
      // qualifies all of them instead.
      group.uncheckable = classifyProbe(probe) !== 'working';
    } else {
      group.counts[classifyProbe(probe)] += 1;
    }
  }
  for (const group of groups.values()) {
    const worst = VERDICT_ORDER.find(h => group.counts[h] > 0) ?? 'notConnected';
    // A failed sweep means every integration result for this user is stale,
    // including a prior DOWN result. Do not claim that a connection is
    // currently broken when the current check could not answer.
    group.worst = group.uncheckable ? 'unknown' : worst;
    // The sweep-check row first: whether the user could be checked at all
    // qualifies every row under it.
    group.rows.sort(([a], [b]) => {
      const checkFirst =
        Number(b.startsWith(INTEGRATION_CHECK_PREFIX)) -
        Number(a.startsWith(INTEGRATION_CHECK_PREFIX));
      return checkFirst || a.localeCompare(b);
    });
  }
  return [...groups.values()].sort(
    (a, b) =>
      VERDICT_ORDER.indexOf(a.worst) - VERDICT_ORDER.indexOf(b.worst) ||
      b.counts[b.worst] - a.counts[a.worst] ||
      a.label.localeCompare(b.label),
  );
}

function groupVerdict(group: UserGroup): { text: string; className: string; dot: string } {
  if (group.worst === 'failing') {
    return {
      text: `${group.counts.failing} not working`,
      className: 'bg-error-bg text-error-text',
      dot: 'bg-danger',
    };
  }
  if (group.worst === 'unknown') {
    return {
      // "Cannot be checked" and "connected but the check has not settled" are
      // both unknown, and the row detail below says which.
      text: group.uncheckable ? 'Status unknown' : `${group.counts.unknown} unknown`,
      className: 'bg-warning-bg text-warning-text',
      dot: 'bg-warning',
    };
  }
  if (group.worst === 'working') {
    return {
      text: `All ${group.counts.working} working`,
      className: 'bg-success-bg text-success-text',
      dot: 'bg-success',
    };
  }
  return {
    text: 'Nothing connected',
    className: 'bg-panel text-muted-foreground',
    dot: 'bg-muted-foreground/40',
  };
}

/** The one line to read first: how many accounts need attention, out of how many. */
function headlineFor(total: number, failing: number, unknown: number): string {
  if (failing === 0 && unknown === 0) {
    return `No problems across ${total} user${total === 1 ? '' : 's'}.`;
  }
  const parts: string[] = [];
  if (failing > 0) {
    parts.push(
      `${failing} of ${total} users ${failing === 1 ? 'has' : 'have'} an integration that stopped working`,
    );
  }
  if (unknown > 0) {
    // "X of N users" only on the leading clause; a trailing one reads off the
    // same denominator.
    const subject = parts.length ? `${unknown}` : `${unknown} of ${total} users`;
    parts.push(`${subject} ${unknown === 1 ? 'has' : 'have'} an unknown status`);
  }
  return `${parts.join(', ')}.`;
}

/** Users needing attention open themselves; healthy ones stay one line. */
function defaultOpen(group: UserGroup): boolean {
  return group.worst === 'failing' || group.worst === 'unknown';
}

/** "3 working · 1 not working · 2 not connected". Only non-zero parts. */
function groupBreakdown(group: UserGroup): string {
  const parts: string[] = [];
  if (group.uncheckable) parts.push('could not be checked');
  if (group.counts.working) parts.push(`${group.counts.working} working`);
  if (group.counts.failing) parts.push(`${group.counts.failing} not working`);
  if (group.counts.unknown) parts.push(`${group.counts.unknown} unknown`);
  if (group.counts.notConnected) parts.push(`${group.counts.notConnected} not connected`);
  if (!parts.length) return 'no checks recorded';
  // Without this the row reads "could not be checked · 1 working", which
  // contradicts itself. Those counts are the last answer the sweep got, not a
  // current one.
  return group.uncheckable && parts.length > 1
    ? `${parts.join(' · ')} (last known)`
    : parts.join(' · ');
}

function UserIntegrationGroup({
  group,
  threshold,
  open,
  onToggle,
}: {
  group: UserGroup;
  threshold: number;
  open: boolean;
  onToggle: () => void;
}) {
  const verdict = groupVerdict(group);
  const breakdown = groupBreakdown(group);
  return (
    <div className="border-b border-border last:border-b-0" role="group" aria-label={group.label}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        // The accessible name carries the verdict, so the at-a-glance reading
        // survives for anyone who cannot see the pill's colour.
        aria-label={`${group.label}: ${verdict.text}. ${breakdown}`}
        className="w-full flex items-center justify-between gap-3 py-3 min-h-[44px] text-left cursor-pointer"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span
            className={`inline-block w-2 h-2 rounded-full shrink-0 ${verdict.dot}`}
            aria-hidden
          />
          <span className="min-w-0">
            <span className="block text-sm font-medium truncate">{group.label}</span>
            <span className="block text-[11px] text-muted-foreground truncate">{breakdown}</span>
          </span>
        </span>
        <span className="flex items-center gap-2 shrink-0">
          <span className={`${PILL_CLASS} ${verdict.className}`}>{verdict.text}</span>
          <span className="text-xs text-muted-foreground" aria-hidden>
            {open ? '▾' : '▸'}
          </span>
        </span>
      </button>
      {open && (
        <div className="pl-4 pb-1 border-l-2 border-border ml-1">
          {group.rows.map(([key, probe]) => (
            <ProbeRow
              key={key}
              probeKey={key}
              probe={probe}
              threshold={threshold}
              // The group heading already names the user; repeating it on every
              // row is what made the flat list unreadable.
              title={probe.integration || 'Integration checks'}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone = 'default',
  subtitle,
}: {
  label: string;
  value: string | number;
  tone?: 'default' | 'good' | 'bad';
  subtitle?: string;
}) {
  const valueClass =
    tone === 'bad' ? 'text-danger' : tone === 'good' ? 'text-success' : 'text-foreground';
  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-xl font-bold font-display ${valueClass}`}>{value}</p>
      {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
    </div>
  );
}

function stepPillClass(status: RunStepStatus): string {
  if (status === 'ok') return 'bg-success-bg text-success-text';
  if (status === 'failed') return 'bg-error-bg text-error-text';
  if (status === 'running') return 'bg-info-bg text-info-text';
  return 'bg-panel text-muted-foreground';
}

function stepLabel(status: RunStepStatus): string {
  if (status === 'ok') return 'Passed';
  if (status === 'failed') return 'Failed';
  if (status === 'running') return 'Checking…';
  return 'Queued';
}

/** "820ms" under a second, "12.4s" above it. Durations here span both. */
function formatElapsed(ms: number | null): string {
  if (ms === null) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function RunStepRow({ step }: { step: MonitoringRunStep }) {
  return (
    <div className="flex flex-col gap-1 py-2.5 border-b border-border last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium min-w-0 break-words">{step.label}</p>
        <div className="flex items-center gap-2 shrink-0">
          {step.elapsed_ms !== null && (
            <span className="text-[11px] text-muted-foreground font-mono">
              {formatElapsed(step.elapsed_ms)}
            </span>
          )}
          <span
            className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[--radius-full] text-[11px] font-semibold whitespace-nowrap ${stepPillClass(step.status)}`}
          >
            {step.status === 'running' && (
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-info animate-pulse" aria-hidden />
            )}
            {stepLabel(step.status)}
          </span>
        </div>
      </div>
      {step.detail && (
        <p className="text-xs text-muted-foreground break-words">{step.detail}</p>
      )}
    </div>
  );
}

function RunProgressCard({ run }: { run: MonitoringRun }) {
  const done = run.steps.filter(s => s.status === 'ok' || s.status === 'failed').length;
  const failed = run.steps.filter(s => s.status === 'failed').length;
  return (
    <section aria-label="Probe run progress">
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <h3 className="text-sm font-semibold">
          {run.running ? 'Run in progress' : 'Last run'}
        </h3>
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {run.running
            ? `${done} of ${run.steps.length} checks done`
            : `${run.steps.length} checks, ${failed} failed`}
          {' · '}
          <span title={formatAbsolute(run.started_at)}>
            {run.trigger === 'manual' ? 'started' : 'scheduled'} {formatRelative(run.started_at)}
          </span>
        </p>
      </div>
      {run.error && (
        <p className="text-xs text-error-text bg-error-bg border border-error-border rounded-[--radius-md] p-2 mb-2 break-words">
          The run itself failed: {run.error}
        </p>
      )}
      <div className="bg-card border border-border rounded-[--radius-md] px-3">
        {run.steps.map(step => (
          <RunStepRow key={step.key} step={step} />
        ))}
      </div>
    </section>
  );
}

function EmailDeliveryCard({
  email,
  alertsEnabled,
}: {
  email: MonitoringEmail;
  alertsEnabled: boolean;
}) {
  const [testing, setTesting] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [message, setMessage] = useState('');
  const [diagnostics, setDiagnostics] = useState<EmailDiagnostics | null>(null);

  const handleTestAlert = useCallback(async () => {
    setTesting(true);
    setMessage('');
    try {
      const result = await sendMonitoringTestAlert();
      setMessage(result.detail);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Failed to send test alert');
    } finally {
      setTesting(false);
    }
  }, []);

  const handleDiagnose = useCallback(async () => {
    setDiagnosing(true);
    setMessage('');
    try {
      setDiagnostics(await diagnoseEmailDelivery());
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Failed to run the email delivery diagnostic');
    } finally {
      setDiagnosing(false);
    }
  }, []);

  return (
    <section>
      <h3 className="text-sm font-semibold mb-1">Email delivery</h3>
      <p className="text-xs text-muted-foreground mb-2">
        Every alert above is delivered over this transport, so a broken one hides every
        failure it was supposed to report. The diagnostic runs from inside this container
        and separates a blocked network path from a mail server saying no.
      </p>
      <div className="bg-card border border-border rounded-[--radius-md] p-3 flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          {email.configured ? (
            <>
              SMTP{' '}
              <span className="font-mono text-foreground">
                {email.host}:{email.port}
              </span>{' '}
              &middot; {email.timeout_seconds}s timeout &middot; alerts{' '}
              {alertsEnabled ? 'on' : 'off'}
            </>
          ) : (
            'SMTP is not configured, so nothing is emailed. Set SMTP_HOST and SMTP_FROM_EMAIL.'
          )}
        </p>
        <p className="text-xs text-muted-foreground">
          {email.last_success_at ? (
            <span title={formatAbsolute(email.last_success_at)}>
              Last successful send {formatRelative(email.last_success_at)}.
            </span>
          ) : (
            'No successful send recorded in this process.'
          )}
        </p>
        {email.last_error && (
          <p className="text-xs text-error-text bg-error-bg border border-error-border rounded-[--radius-md] p-2 break-words">
            <span title={formatAbsolute(email.last_error_at)}>
              Last attempt failed {formatRelative(email.last_error_at)}:
            </span>{' '}
            {email.last_error}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3 pt-1">
          <button
            type="button"
            onClick={() => void handleDiagnose()}
            disabled={diagnosing}
            className="px-3 py-2 min-h-[44px] text-sm font-medium rounded-[--radius-md] border border-border bg-card hover:bg-secondary-hover disabled:opacity-60 cursor-pointer"
          >
            {diagnosing ? 'Diagnosing…' : 'Diagnose delivery'}
          </button>
          <button
            type="button"
            onClick={() => void handleTestAlert()}
            disabled={testing}
            className="text-xs text-muted-foreground hover:text-foreground underline disabled:opacity-60 cursor-pointer min-h-[44px]"
          >
            {testing ? 'Sending…' : 'Send test alert'}
          </button>
          {message && (
            <p className="text-xs text-muted-foreground break-words" role="status">
              {message}
            </p>
          )}
        </div>

        {diagnostics && (
          <div className="flex flex-col gap-2 pt-1">
            <p
              className={`text-xs rounded-[--radius-md] p-2 break-words ${
                diagnostics.handshake_ok
                  ? 'bg-success-bg text-success-text border border-border'
                  : 'bg-warning-bg text-warning-text border border-warning-border'
              }`}
            >
              {diagnostics.conclusion}
            </p>
            {diagnostics.ports.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {diagnostics.ports.map(port => (
                  <span
                    key={port.port}
                    title={port.detail}
                    className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[--radius-full] text-[11px] font-mono ${
                      port.reachable
                        ? 'bg-success-bg text-success-text'
                        : 'bg-panel text-muted-foreground'
                    }`}
                  >
                    <span
                      className={`inline-block w-1.5 h-1.5 rounded-full ${port.reachable ? 'bg-success' : 'bg-muted-foreground/40'}`}
                      aria-hidden
                    />
                    {port.port}
                    {port.port === diagnostics.port ? ' (configured)' : ''}
                  </span>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground break-words">
              Handshake: {diagnostics.handshake_detail}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

export default function MonitoringTab() {
  const [status, setStatus] = useState<MonitoringStatus | null>(null);
  const [error, setError] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [actionMessage, setActionMessage] = useState<string>('');
  const [showHealthyIntegrations, setShowHealthyIntegrations] = useState(false);
  // Explicit expand/collapse choices only. Untouched groups fall back to the
  // data-driven default (failing users open), so a user who breaks between two
  // polls opens itself rather than staying collapsed behind a stale flag.
  const [expandedUsers, setExpandedUsers] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      setStatus(await getMonitoringStatus());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load monitoring status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = status?.health_monitor.run ?? null;
  const runInFlight = run?.running ?? false;

  // Poll while a run is in flight, including a run somebody else started or the
  // scheduled tick, so the steps advance on their own. Keyed on ``status`` so
  // each completed poll schedules the next and the chain stops when the run
  // finishes.
  useEffect(() => {
    if (!runInFlight) return;
    const timer = setTimeout(() => void load(), RUN_POLL_MS);
    return () => clearTimeout(timer);
  }, [status, runInFlight, load]);

  const handleRunProbes = useCallback(async () => {
    setStarting(true);
    setActionMessage('');
    try {
      const result = await runHealthProbes();
      setActionMessage(result.detail);
      await load();
    } catch (e) {
      setActionMessage(e instanceof Error ? e.message : 'Failed to run probes');
    } finally {
      setStarting(false);
    }
  }, [load]);

  if (loading) {
    return <div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />;
  }
  if (error || !status) {
    return (
      <div className="bg-error-bg text-error-text border border-error-border rounded-[--radius-md] p-3 text-sm">
        {error || 'No monitoring data.'}
      </div>
    );
  }

  const probes = Object.entries(status.health_monitor.probes);
  const isPerUser = (key: string): boolean =>
    key.startsWith(INTEGRATION_PREFIX) || key.startsWith(INTEGRATION_CHECK_PREFIX);
  const infraProbes = probes.filter(([key]) => !isPerUser(key));
  const integrationProbes = probes.filter(([key]) => isPerUser(key));
  // ``never_connected`` rows are DOWN in the backend's state machine but are
  // not breakage: nobody ever connected them. Counting them would put several
  // hundred "failures" on a perfectly healthy multi-tenant deployment and bury
  // the infrastructure rows this tab exists to surface.
  const isFailing = (p: MonitoringProbe): boolean => p.status === 'down' && !p.never_connected;
  const downCount = probes.filter(([, p]) => isFailing(p)).length;
  const unconnectedCount = integrationProbes.filter(([, p]) => p.never_connected).length;
  const history = status.health_monitor.history;

  const userGroups = groupByUser(integrationProbes);
  const problemGroups = userGroups.filter(g => g.worst === 'failing' || g.worst === 'unknown');
  const visibleGroups = showHealthyIntegrations ? userGroups : problemGroups;
  const failingUsers = userGroups.filter(g => g.worst === 'failing').length;
  const unknownUsers = userGroups.filter(g => g.worst === 'unknown').length;

  return (
    <div className="flex flex-col gap-6">
      {/* Alerting configuration. Dormant pipelines are the failure nobody
          notices until an incident, so this leads rather than hides below. */}
      {(!status.health_monitor.enabled || !status.alerts.enabled) && (
        <div className="bg-warning-bg text-warning-text border border-warning-border rounded-[--radius-md] p-3 text-sm">
          <p className="font-semibold mb-1">Email alerting is not fully active.</p>
          <p>
            {!status.recipient_configured
              ? 'No recipient resolves: set ALERT_EMAIL or ADMIN_EMAIL.'
              : 'SMTP is not configured, so probe results and error alerts are visible here but are not emailed.'}
          </p>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <SummaryCard
          label="Failing checks"
          value={downCount}
          tone={downCount > 0 ? 'bad' : 'good'}
          subtitle={`${probes.length} total`}
        />
        <SummaryCard
          label="Probes last ran"
          value={
            status.health_monitor.last_run_at
              ? formatRelative(status.health_monitor.last_run_at)
              : 'never'
          }
          subtitle={`every ${status.health_monitor.interval_seconds}s`}
        />
        <SummaryCard
          label="Error alerts"
          value={status.alerts.enabled ? 'on' : 'off'}
          subtitle={`${status.alerts.pending_groups} pending group${status.alerts.pending_groups === 1 ? '' : 's'}`}
        />
        <SummaryCard
          label="Failure threshold"
          value={status.health_monitor.failure_threshold}
          subtitle="checks before DOWN"
        />
      </div>

      <div className="flex flex-wrap gap-3 items-center">
        <button
          type="button"
          onClick={() => void handleRunProbes()}
          disabled={starting || runInFlight}
          className="px-3 py-2 min-h-[44px] text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-60 cursor-pointer"
        >
          {runInFlight ? 'Running…' : 'Run safe probes now'}
        </button>
        {actionMessage && (
          <p className="text-xs text-muted-foreground" role="status">
            {actionMessage}
          </p>
        )}
      </div>

      {run && <RunProgressCard run={run} />}

      <section>
        <h3 className="text-sm font-semibold mb-1">Infrastructure</h3>
        <p className="text-xs text-muted-foreground mb-2">
          Dependencies that are not probed at all do not appear here: an unconfigured
          integration is absent rather than permanently red.
        </p>
        <div className="bg-card border border-border rounded-[--radius-md] px-3">
          {infraProbes.length === 0 ? (
            <p className="py-3 text-sm text-muted-foreground">
              No probes have run yet in this process.
            </p>
          ) : (
            infraProbes.map(([key, probe]) => (
              <ProbeRow
                key={key}
                probeKey={key}
                probe={probe}
                threshold={status.health_monitor.failure_threshold}
              />
            ))
          )}
        </div>
      </section>

      <section>
        <div className="flex items-center justify-between gap-3 mb-1">
          <h3 className="text-sm font-semibold">Per-user integrations</h3>
          {userGroups.length > 0 && (
            <button
              type="button"
              onClick={() => setShowHealthyIntegrations(v => !v)}
              className="text-xs text-primary hover:underline cursor-pointer"
            >
              {showHealthyIntegrations
                ? 'Show only users with problems'
                : `Show all ${userGroups.length} users`}
            </button>
          )}
        </div>
        <p className="text-xs text-muted-foreground mb-2" aria-live="polite">
          {userGroups.length > 0 && (
            <>
              <span className="text-foreground font-medium">{headlineFor(userGroups.length, failingUsers, unknownUsers)}</span>{' '}
            </>
          )}
          An integration a user never connected reports the same reason as one whose token
          expired, so these alert only when a working connection stops working.{' '}
          {unconnectedCount === 1
            ? 'The 1 never-connected one shows as Not connected and does not count against a user.'
            : `The ${unconnectedCount} never-connected ones show as Not connected and do not count against a user.`}
        </p>
        <div className="bg-card border border-border rounded-[--radius-md] px-3">
          {visibleGroups.length === 0 ? (
            <p className="py-3 text-sm text-muted-foreground">
              {userGroups.length === 0
                ? 'No integration checks have run yet.'
                : 'Every user checked is either working or has nothing connected.'}
            </p>
          ) : (
            visibleGroups.map(group => (
              <UserIntegrationGroup
                key={group.userId}
                group={group}
                threshold={status.health_monitor.failure_threshold}
                open={expandedUsers[group.userId] ?? defaultOpen(group)}
                onToggle={() =>
                  setExpandedUsers(prev => ({
                    ...prev,
                    [group.userId]: !(prev[group.userId] ?? defaultOpen(group)),
                  }))
                }
              />
            ))
          )}
        </div>
      </section>

      <section>
        <h3 className="text-sm font-semibold mb-1">Activity</h3>
        <p className="text-xs text-muted-foreground mb-2">
          Status changes and self-repairs since this process started. Kept in memory, so a
          deploy resets it; the alert emails are the durable record.
        </p>
        <div className="bg-card border border-border rounded-[--radius-md] px-3">
          {history.length === 0 ? (
            <p className="py-3 text-sm text-muted-foreground">
              Nothing has changed state yet.
            </p>
          ) : (
            history.map((event, i) => (
              <div
                key={`${event.at}-${event.key}-${i}`}
                className="flex flex-col gap-1 py-3 border-b border-border last:border-b-0"
              >
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium min-w-0 truncate">{event.label}</p>
                  <StatusPill status={event.status} />
                </div>
                {event.detail && (
                  <p className="text-xs text-muted-foreground break-words">{event.detail}</p>
                )}
                <p className="text-[11px] text-muted-foreground" title={formatAbsolute(event.at)}>
                  {formatRelative(event.at)}
                </p>
              </div>
            ))
          )}
        </div>
      </section>

      <EmailDeliveryCard email={status.email} alertsEnabled={status.alerts.enabled} />
    </div>
  );
}
