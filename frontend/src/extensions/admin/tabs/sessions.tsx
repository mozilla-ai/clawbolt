import { useState, useEffect, useCallback } from 'react';
import {
  getSessions,
  type SessionListItem,
} from '../admin-api';

export default function SessionsTab() {
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getSessions(50)
      .then((res) => { setSessions(res.items); setTotal(res.total); })
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

  if (sessions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No sessions yet. Start a conversation to see activity here.
      </p>
    );
  }

  return (
    <div>
      <p className="text-xs text-muted-foreground mb-2">
        {total} total session{total !== 1 ? 's' : ''}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              <th className="py-2 px-2 font-medium">Channel</th>
              <th className="py-2 px-2 font-medium">Messages</th>
              <th className="py-2 px-2 font-medium">Last Activity</th>
              <th className="py-2 px-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map(s => (
              <tr key={s.session_id} className="border-b border-border/50">
                <td className="py-2 px-2 text-xs">{s.channel || 'unknown'}</td>
                <td className="py-2 px-2 text-xs">{s.message_count}</td>
                <td className="py-2 px-2 text-xs">
                  {new Date(s.last_message_at).toLocaleString()}
                </td>
                <td className="py-2 px-2">
                  <span className={`text-xs px-1.5 py-0.5 rounded-[--radius-full] ${
                    s.is_active
                      ? 'bg-success-bg text-success-text'
                      : 'bg-secondary text-muted-foreground'
                  }`}>
                    {s.is_active ? 'active' : 'closed'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
