import { useState, useEffect, useCallback } from 'react';
import {
  getHeartbeatLogs,
  type HeartbeatLogItem,
} from '../admin-api';

function ExpandableText({ text, label }: { text: string; label: string }) {
  const [expanded, setExpanded] = useState(false);
  if (!text) return null;
  const preview = text.length > 80 ? text.slice(0, 80) + '...' : text;
  return (
    <div className="mt-1">
      <span className="text-[10px] text-muted-foreground uppercase">{label}: </span>
      <span className="text-xs">
        {expanded ? text : preview}
        {text.length > 80 && (
          <button
            className="text-primary hover:underline ml-1 text-[10px]"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? 'less' : 'more'}
          </button>
        )}
      </span>
    </div>
  );
}

export function HeartbeatEntry({ log }: { log: HeartbeatLogItem }) {
  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-muted-foreground">
          {new Date(log.created_at).toLocaleString()}
        </span>
        <span className={`text-[10px] px-1.5 py-0.5 rounded-[--radius-full] font-medium ${
          log.action_type === 'send'
            ? 'bg-success-bg text-success-text'
            : 'bg-secondary text-muted-foreground'
        }`}>
          {log.action_type === 'send' ? 'Sent' : 'Skipped'}
        </span>
        {log.channel && (
          <span className="text-[10px] px-1.5 py-0.5 rounded-[--radius-full] bg-primary-light text-primary">
            {log.channel}
          </span>
        )}
      </div>
      <ExpandableText text={log.message_text} label="message" />
      <ExpandableText text={log.reasoning} label="reasoning" />
      {log.tasks && <ExpandableText text={log.tasks} label="tasks" />}
    </div>
  );
}

export default function HeartbeatsTab() {
  const [logs, setLogs] = useState<HeartbeatLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getHeartbeatLogs(100)
      .then((res) => { setLogs(res.items); setTotal(res.total); })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;

  if (error) {
    return (
      <div className="text-danger text-sm">
        {error}{' '}
        <button className="text-primary hover:underline" onClick={load}>Retry</button>
      </div>
    );
  }

  if (logs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No heartbeat activity yet. Heartbeats are sent when your assistant checks in based on your schedule.
      </p>
    );
  }

  return (
    <div>
      <p className="text-xs text-muted-foreground mb-2">
        {total} total heartbeat event{total !== 1 ? 's' : ''}
      </p>
      <div className="space-y-2">
        {logs.map(log => (
          <HeartbeatEntry key={log.id} log={log} />
        ))}
      </div>
    </div>
  );
}
