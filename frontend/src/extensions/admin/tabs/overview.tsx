import { useState, useEffect, useCallback, useMemo, type ReactNode } from 'react';
import {
  getAdminStats,
  getAdminVersion,
  getMonitoringStatus,
  getSharedDataSummary,
  getSharedDataUsers,
  type AdminStats,
  type AdminVersion,
  type MonitoringStatus,
  type SharedDataSummary,
  type SharedDataUser,
} from '../admin-api';
import { formatAbsolute, formatRelative } from '../format';

// The admin landing page (#662). Admins are routed here on login, so it has
// to answer three questions without a click:
//
//   1. Does anything need me right now?  -> "Needs attention"
//   2. Are systems and integrations healthy? -> "System health"
//   3. When did consenting users last talk to Clawbolt? -> "Shared activity"
//
// Every panel loads independently. A failure in one (monitoring is the
// flakiest: it reaches out to third-party dependencies) must never blank the
// others, so each has its own error state rather than one page-level catch.

/** How many consenting users the shared-conversations table shows before "see all". */
const SHARED_USER_PREVIEW = 8;

interface OverviewTabProps {
  /** Navigate to another admin section by slug. */
  onGoToSection?: (slug: string) => void;
  /** Drill into a user's detail page, optionally straight to a sub-view. */
  onSelectUserById?: (userId: string, section?: string) => void;
}

// ---------------------------------------------------------------------------
// Presentational primitives
// ---------------------------------------------------------------------------

function Panel({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="bg-card border border-border rounded-[--radius-md] p-4">
      <header className="flex items-center justify-between gap-2 flex-wrap mb-3">
        <h3 className="text-sm font-semibold">{title}</h3>
        {aside}
      </header>
      {children}
    </section>
  );
}

function PanelSkeleton({ title, height = 'h-24' }: { title: string; height?: string }) {
  return (
    <Panel title={title}>
      <div className={`animate-pulse ${height} bg-panel rounded-[--radius-sm]`} />
    </Panel>
  );
}

function PanelError({ title, message }: { title: string; message: string }) {
  return (
    <Panel title={title}>
      <p className="text-xs text-danger">{message}</p>
    </Panel>
  );
}

function LinkButton({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  if (!onClick) return null;
  return (
    <button type="button" className="text-xs text-primary hover:underline" onClick={onClick}>
      {children}
    </button>
  );
}

function StatusDot({ tone }: { tone: 'ok' | 'warn' | 'bad' | 'idle' }) {
  const map = {
    ok: 'bg-success',
    warn: 'bg-warning',
    bad: 'bg-danger',
    idle: 'bg-muted-foreground/40',
  } as const;
  return <span className={`inline-block w-2 h-2 shrink-0 rounded-full ${map[tone]}`} aria-hidden />;
}

function HealthRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'ok' | 'warn' | 'bad' | 'idle';
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={`flex items-center gap-1.5 text-right ${
          tone === 'ok' ? 'text-foreground' : tone === 'idle' ? 'text-muted-foreground' : ''
        }`}
      >
        <StatusDot tone={tone} />
        {value}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Needs attention
// ---------------------------------------------------------------------------

interface AttentionItem {
  key: string;
  tone: 'warn' | 'bad';
  text: string;
  actionLabel: string;
  slug: string;
}

/**
 * Everything on this page that is a to-do rather than a number, collected
 * into one strip at the top. Empty means empty: an "all clear" row beats a
 * panel of zeros, which reads as a stale dashboard.
 */
function buildAttentionItems(
  stats: AdminStats | null,
  summary: SharedDataSummary | null,
  monitoring: MonitoringStatus | null,
): AttentionItem[] {
  const items: AttentionItem[] = [];

  const reports = summary?.open_reports_count ?? 0;
  if (reports > 0) {
    items.push({
      key: 'reports',
      tone: 'warn',
      text: `${reports} reported conversation${reports === 1 ? '' : 's'} open`,
      actionLabel: 'Triage reports',
      slug: 'reported',
    });
  }

  // never_connected probes are DOWN only because nobody linked the
  // integration. Counting them would make the strip permanently red.
  const downProbes = Object.values(monitoring?.health_monitor.probes ?? {}).filter(
    p => p.status === 'down' && !p.never_connected,
  );
  if (downProbes.length > 0) {
    items.push({
      key: 'probes',
      tone: 'bad',
      text:
        downProbes.length === 1 && downProbes[0]
          ? `${downProbes[0].label} is down`
          : `${downProbes.length} dependencies are down`,
      actionLabel: 'Open monitoring',
      slug: 'monitoring',
    });
  }

  if (monitoring && !monitoring.recipient_configured) {
    items.push({
      key: 'alerts',
      tone: 'warn',
      text: 'Alert emails have no recipient configured',
      actionLabel: 'Open monitoring',
      slug: 'monitoring',
    });
  }

  const noChannels =
    stats != null &&
    !stats.telegram_configured &&
    !stats.bluebubbles_configured &&
    !stats.twilio_configured;
  if (noChannels) {
    items.push({
      key: 'channels',
      tone: 'bad',
      text: 'No messaging channel is configured, so nobody can reach the assistant',
      actionLabel: 'Open config',
      slug: 'config',
    });
  }

  return items;
}

function AttentionPanel({
  items,
  ready,
  onGoToSection,
}: {
  items: AttentionItem[];
  ready: boolean;
  onGoToSection?: (slug: string) => void;
}) {
  if (!ready) return <PanelSkeleton title="Needs attention" height="h-16" />;

  if (items.length === 0) {
    return (
      <Panel title="Needs attention">
        <p className="text-sm text-muted-foreground flex items-center gap-2">
          <StatusDot tone="ok" />
          Nothing needs attention. No open reports or dependency alarms.
        </p>
      </Panel>
    );
  }

  return (
    <Panel title="Needs attention">
      <ul className="divide-y divide-border/50">
        {items.map(item => (
          <li
            key={item.key}
            // Stacks on narrow viewports: truncating the sentence to fit an
            // action link beside it hides the part that says what is wrong.
            className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1 sm:gap-3 py-2 first:pt-0"
          >
            <span className="flex items-center gap-2 text-sm min-w-0">
              <StatusDot tone={item.tone} />
              <span className="sm:truncate">{item.text}</span>
            </span>
            <div className="pl-4 sm:pl-0 shrink-0">
              <LinkButton onClick={onGoToSection && (() => onGoToSection(item.slug))}>
                {item.actionLabel}
              </LinkButton>
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Shared activity
// ---------------------------------------------------------------------------

/**
 * The pilot's front door: who opted in, when they last spoke with Clawbolt,
 * and one click into their activity. Recent conversations appear first.
 */
function SharedConversationsPanel({
  summary,
  users,
  error,
  onGoToSection,
  onSelectUserById,
}: {
  summary: SharedDataSummary | null;
  users: SharedDataUser[] | null;
  error: string | null;
  onGoToSection?: (slug: string) => void;
  onSelectUserById?: (userId: string, section?: string) => void;
}) {
  const rows = useMemo(() => {
    if (!users) return [];
    return users
      .sort(
        (a, b) =>
          (b.last_message_at ?? '').localeCompare(a.last_message_at ?? '') ||
          b.conversation_count - a.conversation_count ||
          a.email.localeCompare(b.email),
      );
  }, [users]);

  if (error) return <PanelError title="Shared activity" message={error} />;
  if (!summary || !users) return <PanelSkeleton title="Shared activity" height="h-40" />;

  if (summary.consenting_user_count === 0) {
    return (
      <Panel title="Shared activity">
        <p className="text-sm text-muted-foreground">
          No users have opted into research data sharing yet. Once someone opts in from
          their Account page, their conversations, memory, and heartbeat activity become
          readable here.
        </p>
      </Panel>
    );
  }

  const preview = rows.slice(0, SHARED_USER_PREVIEW);

  return (
    <Panel
      title="Shared activity"
      aside={
        <span className="text-xs text-muted-foreground">
          {summary.consenting_user_count} sharing data
        </span>
      }
    >

      {preview.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          Consenting users have no conversations recorded yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="py-1.5 pr-2 font-medium">User</th>
                <th className="py-1.5 px-2 font-medium">Last message</th>
                <th className="py-1.5 px-2 font-medium">Shared since</th>
                <th className="py-1.5 pl-2 font-medium text-right">Open</th>
              </tr>
            </thead>
            <tbody>
              {preview.map(u => (
                <tr key={u.id} className="border-b border-border/50">
                  <td className="py-1.5 pr-2 max-w-[240px]">
                    <button
                      type="button"
                      className="text-left w-full truncate hover:underline decoration-dotted underline-offset-2 disabled:cursor-default disabled:hover:no-underline"
                      disabled={!onSelectUserById}
                      onClick={() => onSelectUserById?.(u.id, 'activity')}
                    >
                      {u.email || u.user_id}
                    </button>
                  </td>
                  <td className="py-1.5 px-2 text-muted-foreground text-xs">
                    {u.last_message_at ? (
                      <span title={formatAbsolute(u.last_message_at)}>
                        {formatRelative(u.last_message_at) || formatAbsolute(u.last_message_at)}
                      </span>
                    ) : (
                      'No messages yet'
                    )}
                  </td>
                  <td className="py-1.5 px-2 text-muted-foreground text-xs">
                    <span title={formatAbsolute(u.consent_at)}>
                      {formatRelative(u.consent_at) || '—'}
                    </span>
                  </td>
                  <td className="py-1.5 pl-2 text-right whitespace-nowrap">
                    <LinkButton onClick={onSelectUserById && (() => onSelectUserById(u.id, 'activity'))}>
                      Activity
                    </LinkButton>
                    <span className="text-muted-foreground text-xs mx-1.5">·</span>
                    <LinkButton onClick={onSelectUserById && (() => onSelectUserById(u.id, 'memory'))}>
                      Memory
                    </LinkButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > preview.length && (
        <p className="mt-3 text-xs text-muted-foreground">
          Showing {preview.length} of {rows.length} consenting users.{' '}
          <LinkButton onClick={onGoToSection && (() => onGoToSection('users'))}>
            See all in Users
          </LinkButton>
        </p>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// System health
// ---------------------------------------------------------------------------

function SystemHealthPanel({
  stats,
  monitoring,
  monitoringError,
  onGoToSection,
}: {
  stats: AdminStats;
  monitoring: MonitoringStatus | null;
  monitoringError: string | null;
  onGoToSection?: (slug: string) => void;
}) {
  const probes = Object.entries(monitoring?.health_monitor.probes ?? {});
  const down = probes.filter(([, p]) => p.status === 'down' && !p.never_connected);
  const unknown = probes.filter(([, p]) => p.status === 'unknown');
  const lastRun = monitoring?.health_monitor.last_run_at ?? null;

  let probeTone: 'ok' | 'warn' | 'bad' | 'idle' = 'idle';
  let probeValue = 'Not run yet';
  if (monitoringError) {
    probeTone = 'idle';
    probeValue = 'Unavailable';
  } else if (probes.length > 0) {
    if (down.length > 0) {
      probeTone = 'bad';
      probeValue = `${down.length} of ${probes.length} down`;
    } else if (unknown.length > 0) {
      probeTone = 'warn';
      probeValue = `${probes.length - unknown.length} of ${probes.length} up`;
    } else {
      probeTone = 'ok';
      probeValue = `All ${probes.length} up`;
    }
  }

  return (
    <Panel
      title="System and integration health"
      aside={<LinkButton onClick={onGoToSection && (() => onGoToSection('monitoring'))}>Monitoring</LinkButton>}
    >
      <div className="divide-y divide-border/50">
        <HealthRow
          label="Dependency probes"
          value={probeValue}
          tone={probeTone}
        />
        {lastRun && (
          <HealthRow
            label="Last probe run"
            value={formatRelative(lastRun) || formatAbsolute(lastRun)}
            tone="idle"
          />
        )}
        <HealthRow
          label="Telegram"
          value={stats.telegram_configured ? 'Configured' : 'Not configured'}
          tone={stats.telegram_configured ? 'ok' : 'idle'}
        />
        <HealthRow
          label="BlueBubbles (iMessage)"
          value={stats.bluebubbles_configured ? 'Configured' : 'Not configured'}
          tone={stats.bluebubbles_configured ? 'ok' : 'idle'}
        />
        <HealthRow
          label="Twilio (RCS + SMS fallback)"
          value={stats.twilio_configured ? 'Configured' : 'Not configured'}
          tone={stats.twilio_configured ? 'ok' : 'idle'}
        />
      </div>
      {down.length > 0 && (
        <ul className="mt-3 text-xs text-danger space-y-0.5">
          {down.map(([key, p]) => (
            <li key={key}>
              {p.label}: {p.detail || 'down'}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Version
// ---------------------------------------------------------------------------

function shortenCommit(commit: string): string {
  if (commit === 'unknown' || commit.length <= 12) return commit;
  return commit.slice(0, 12);
}

function VersionPanel() {
  const [version, setVersion] = useState<AdminVersion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAdminVersion()
      .then(setVersion)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <PanelError title="Version" message={error} />;
  if (!version) return <PanelSkeleton title="Version" height="h-16" />;

  const startedAt = new Date(version.started_at);
  const startedDisplay = isNaN(startedAt.getTime())
    ? version.started_at
    : startedAt.toLocaleString();

  return (
    <Panel title="Version">
      <div className="divide-y divide-border/50 text-sm">
        <VersionRow
          label="Premium"
          primary={version.premium_version}
          secondary={shortenCommit(version.premium_commit)}
        />
        <VersionRow
          label="OSS"
          primary={version.oss_version || shortenCommit(version.oss_commit)}
          secondary={version.oss_version ? shortenCommit(version.oss_commit) : undefined}
        />
        <VersionRow label="Process started" primary={startedDisplay} />
      </div>
    </Panel>
  );
}

function VersionRow({
  label,
  primary,
  secondary,
}: {
  label: string;
  primary: string;
  secondary?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums text-xs text-foreground text-right break-all">
        {primary}
        {secondary ? <span className="ml-2 text-muted-foreground">{secondary}</span> : null}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function OverviewTab({ onGoToSection, onSelectUserById }: OverviewTabProps) {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);

  const [summary, setSummary] = useState<SharedDataSummary | null>(null);
  const [sharedUsers, setSharedUsers] = useState<SharedDataUser[] | null>(null);
  const [sharedError, setSharedError] = useState<string | null>(null);

  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [monitoringError, setMonitoringError] = useState<string | null>(null);

  const load = useCallback(() => {
    setStatsError(null);
    setSharedError(null);
    setMonitoringError(null);

    getAdminStats()
      .then(setStats)
      .catch((e: Error) => setStatsError(e.message));

    // The pilot panel needs both the aggregate summary and the user list.
    // One failure fails the panel, not the page.
    Promise.all([getSharedDataSummary(), getSharedDataUsers({ limit: 200 })])
      .then(([s, list]) => {
        setSummary(s);
        setSharedUsers(list.items);
      })
      .catch((e: Error) => setSharedError(e.message));

    // Monitoring reaches out to third-party dependencies and is the most
    // likely of the three to fail or be slow. Degrade to "Unavailable".
    getMonitoringStatus()
      .then(setMonitoring)
      .catch((e: Error) => setMonitoringError(e.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const attentionReady = stats != null || statsError != null;
  const attentionItems = useMemo(
    () => buildAttentionItems(stats, summary, monitoring),
    [stats, summary, monitoring],
  );

  return (
    <div className="space-y-5">
      <AttentionPanel
        items={attentionItems}
        ready={attentionReady}
        onGoToSection={onGoToSection}
      />

      {statsError && (
        <div className="text-danger text-sm">
          {statsError}{' '}
          <button type="button" className="text-primary hover:underline" onClick={load}>
            Retry
          </button>
        </div>
      )}

      {stats && (
        <SystemHealthPanel
          stats={stats}
          monitoring={monitoring}
          monitoringError={monitoringError}
          onGoToSection={onGoToSection}
        />
      )}

      <SharedConversationsPanel
        summary={summary}
        users={sharedUsers}
        error={sharedError}
        onGoToSection={onGoToSection}
        onSelectUserById={onSelectUserById}
      />

      <VersionPanel />
    </div>
  );
}
