import { useState, useEffect } from 'react';
import {
  getHeartbeatLogs,
  getSessions,
  getLLMUsage,
  getProfile,
  type HeartbeatLogItem,
  type SessionListResponse,
  type LLMUsageSummary,
  type UserProfile,
} from '../admin-api';
import { HeartbeatEntry } from './heartbeats';

// --- Frequency parsing (mirrors backend logic) ---

const NAMED_FREQUENCIES: Record<string, number> = {
  daily: 1440,
  weekdays: 1440,
  weekly: 10080,
};

function parseFrequencyToMinutes(freq: string): number | null {
  const trimmed = freq.trim().toLowerCase();
  if (trimmed in NAMED_FREQUENCIES) return NAMED_FREQUENCIES[trimmed];
  const m = trimmed.match(/^(\d+)\s*([mhd])$/);
  if (!m) return null;
  const value = parseInt(m[1], 10);
  const unit = m[2];
  if (unit === 'm') return Math.max(value, 1);
  if (unit === 'h') return value * 60;
  if (unit === 'd') return value * 1440;
  return null;
}

// --- Health status computation ---

type HealthStatus = 'green' | 'amber' | 'red' | 'gray';

function computeHeartbeatHealth(
  logs: HeartbeatLogItem[],
  profile: UserProfile | null,
): { status: HealthStatus; sentToday: number; lastAgo: string } {
  if (!profile || !profile.heartbeat_opt_in) {
    return { status: 'gray', sentToday: 0, lastAgo: '' };
  }

  const freqMinutes = parseFrequencyToMinutes(profile.heartbeat_frequency) ?? 1440;
  const now = Date.now();
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);

  const sends = logs.filter(l => l.action_type === 'send');
  const sentToday = sends.filter(l => new Date(l.created_at) >= todayStart).length;

  if (sends.length === 0) {
    return { status: 'red', sentToday: 0, lastAgo: '' };
  }

  const lastSend = new Date(sends[0].created_at);
  const minutesSinceLast = (now - lastSend.getTime()) / 60000;

  let lastAgo: string;
  if (minutesSinceLast < 60) {
    lastAgo = `${Math.round(minutesSinceLast)} min ago`;
  } else if (minutesSinceLast < 1440) {
    lastAgo = `${Math.round(minutesSinceLast / 60)}h ago`;
  } else {
    lastAgo = `${Math.round(minutesSinceLast / 1440)}d ago`;
  }

  let status: HealthStatus;
  if (minutesSinceLast <= freqMinutes * 2) {
    status = 'green';
  } else if (minutesSinceLast <= freqMinutes * 4) {
    status = 'amber';
  } else {
    status = 'red';
  }

  return { status, sentToday, lastAgo };
}

// --- Health indicator dot ---

function StatusDot({ status }: { status: HealthStatus }) {
  const colors: Record<HealthStatus, string> = {
    green: 'bg-success',
    amber: 'bg-warning',
    red: 'bg-danger',
    gray: 'bg-muted-foreground',
  };
  return <span className={`inline-block w-2 h-2 rounded-full ${colors[status]}`} />;
}

function heartbeatStatusText(status: HealthStatus): string {
  if (status === 'amber') return 'Overdue';
  if (status === 'red') return 'No recent activity';
  if (status === 'gray') return 'Not configured';
  return '';
}

// --- Data types ---

interface OverviewData {
  heartbeatLogs: HeartbeatLogItem[];
  sessions: SessionListResponse;
  usage: LLMUsageSummary;
  profile: UserProfile | null;
}

// --- Overview Tab ---

export default function OverviewTab({ onSwitchTab }: { onSwitchTab: (id: string) => void }) {
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);

    Promise.all([
      getHeartbeatLogs(200).catch(() => ({ total: 0, items: [] as HeartbeatLogItem[] })),
      getSessions(200).catch(() => ({ total: 0, items: [] })),
      getLLMUsage(30).catch(() => ({ total_calls: 0, total_tokens: 0, total_cost: 0, by_purpose: [] })),
      getProfile().catch(() => null),
    ])
      .then(([heartbeatLogs, sessions, usage, profile]) => {
        setData({ heartbeatLogs: heartbeatLogs.items, sessions, usage, profile });
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="animate-pulse bg-panel rounded-[--radius-md] h-24" />
        <div className="animate-pulse bg-panel rounded-[--radius-md] h-24" />
        <div className="animate-pulse bg-panel rounded-[--radius-md] h-24" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
          {[0, 1, 2].map(i => (
            <div key={i} className="bg-card border border-border rounded-[--radius-md] p-4">
              <p className="text-xl font-semibold font-display">--</p>
              <p className="text-xs text-muted-foreground">Unable to load</p>
            </div>
          ))}
        </div>
        <div className="text-danger text-sm">
          Failed to load overview data.{' '}
          <button className="text-primary hover:underline" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const health = computeHeartbeatHealth(data.heartbeatLogs, data.profile);
  const activeSessions = data.sessions.items.filter(s => s.is_active).length;
  const totalSessions = data.sessions.total;
  const recentActivity = data.heartbeatLogs.slice(0, 10);

  return (
    <div>
      {/* Health cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
        {/* Heartbeat Status */}
        <button
          className="bg-card border border-border rounded-[--radius-md] p-4 text-left cursor-pointer hover:border-primary transition-colors"
          onClick={() => onSwitchTab('heartbeats')}
        >
          <div className="flex items-center gap-2 mb-1">
            <StatusDot status={health.status} />
            <span className="text-xl font-semibold font-display">
              {health.status === 'gray'
                ? 'Not configured'
                : `${health.sentToday} sent today`}
            </span>
          </div>
          {health.status !== 'gray' && (
            <p className="text-xs text-muted-foreground">
              {health.lastAgo ? `Last: ${health.lastAgo}` : 'No sends yet'}
              {heartbeatStatusText(health.status) && (
                <span className={`ml-2 ${
                  health.status === 'amber' ? 'text-warning' :
                  health.status === 'red' ? 'text-danger' : ''
                }`}>
                  {heartbeatStatusText(health.status)}
                </span>
              )}
            </p>
          )}
        </button>

        {/* Sessions */}
        <button
          className="bg-card border border-border rounded-[--radius-md] p-4 text-left cursor-pointer hover:border-primary transition-colors"
          onClick={() => onSwitchTab('sessions')}
        >
          <p className="text-xl font-semibold font-display">
            {activeSessions} active / {totalSessions} total
          </p>
          <p className="text-xs text-muted-foreground">Sessions</p>
        </button>

        {/* LLM Cost */}
        <button
          className="bg-card border border-border rounded-[--radius-md] p-4 text-left cursor-pointer hover:border-primary transition-colors"
          onClick={() => onSwitchTab('usage')}
        >
          <p className="text-xl font-semibold font-display">
            ${data.usage.total_cost.toFixed(2)} <span className="text-sm font-normal">(30d)</span>
          </p>
          <p className="text-xs text-muted-foreground">
            {data.usage.total_calls.toLocaleString()} calls
          </p>
        </button>
      </div>

      {/* Recent activity feed */}
      <h3 className="text-[15px] font-semibold mb-3">Recent Activity</h3>
      {recentActivity.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          No heartbeat activity yet. Heartbeats show up here when your assistant checks in.
        </p>
      ) : (
        <div className="space-y-2">
          {recentActivity.map(log => (
            <HeartbeatEntry key={log.id} log={log} />
          ))}
        </div>
      )}
    </div>
  );
}
