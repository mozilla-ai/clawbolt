import { useState, useEffect, useCallback } from 'react';
import {
  getLLMUsage,
  type LLMUsageSummary,
} from '../admin-api';

export default function UsageTab() {
  const [usage, setUsage] = useState<LLMUsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getLLMUsage(30)
      .then(setUsage)
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

  if (!usage || usage.total_calls === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No LLM usage in the last 30 days.
      </p>
    );
  }

  return (
    <div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="bg-card border border-border rounded-[--radius-md] p-3">
          <p className="text-xs text-muted-foreground">Total Calls</p>
          <p className="text-xl font-bold">{usage.total_calls.toLocaleString()}</p>
        </div>
        <div className="bg-card border border-border rounded-[--radius-md] p-3">
          <p className="text-xs text-muted-foreground">Total Tokens</p>
          <p className="text-xl font-bold">{usage.total_tokens.toLocaleString()}</p>
        </div>
        <div className="bg-card border border-border rounded-[--radius-md] p-3">
          <p className="text-xs text-muted-foreground">Total Cost</p>
          <p className="text-xl font-bold">${usage.total_cost.toFixed(4)}</p>
        </div>
      </div>

      {usage.by_purpose.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="py-2 px-2 font-medium">Purpose</th>
                <th className="py-2 px-2 font-medium">Calls</th>
                <th className="py-2 px-2 font-medium">Input</th>
                <th className="py-2 px-2 font-medium">Output</th>
                <th className="py-2 px-2 font-medium">Cost</th>
              </tr>
            </thead>
            <tbody>
              {usage.by_purpose.map(p => (
                <tr key={p.purpose} className="border-b border-border/50">
                  <td className="py-2 px-2 text-xs">{p.purpose || 'unknown'}</td>
                  <td className="py-2 px-2 text-xs">{p.call_count.toLocaleString()}</td>
                  <td className="py-2 px-2 text-xs">{p.total_input_tokens.toLocaleString()}</td>
                  <td className="py-2 px-2 text-xs">{p.total_output_tokens.toLocaleString()}</td>
                  <td className="py-2 px-2 text-xs">${p.total_cost.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
