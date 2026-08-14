import { useState, useEffect, useCallback, type ReactNode } from 'react';
import type { UsageSummary } from './types';
import { getSubscription } from './api';

export interface ExtensionTab {
  key: string;
  label: string;
}

export function getExtraSettingsTabs(isPremium: boolean, _isAdmin: boolean): ExtensionTab[] {
  const tabs: ExtensionTab[] = [];
  if (isPremium) {
    tabs.push({ key: 'usage', label: 'Usage' });
  }
  return tabs;
}

export function renderPremiumSettingsTab(key: string, _isAdmin: boolean): ReactNode {
  if (key === 'usage') return <UsageTab />;
  return null;
}

export function showOssSettingsTabs(isPremium: boolean, isAdmin: boolean): string[] {
  // Privacy and Channels are user-facing and visible to every account, including non-admin premium users.
  if (!isPremium) return ['model', 'storage', 'channels', 'privacy'];
  // Model selection is admin-only and lives in the admin panel Config tab.
  // Storage settings are also admin-only but remain here (no admin panel equivalent yet).
  return isAdmin
    ? ['storage', 'channels', 'privacy']
    : ['channels', 'privacy'];
}

function UsageTab() {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getSubscription()
      .then(setUsage)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />;
  }

  if (error) {
    return (
      <div className="text-danger text-sm">
        {error}{' '}
        <button className="text-primary hover:underline" onClick={load}>Retry</button>
      </div>
    );
  }

  if (!usage) return null;

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold font-display mb-3">Usage This Month</h3>
        <div className="grid gap-4 sm:grid-cols-2">
          <UsageBar label="Messages" used={usage.messages.used} limit={usage.messages.limit} />
          <UsageBar label="Tokens" used={usage.tokens.used} limit={usage.tokens.limit} />
        </div>
        {usage.period_start && (
          <p className="text-xs text-muted-foreground mt-2">
            Period started: {new Date(usage.period_start).toLocaleDateString()}
          </p>
        )}
      </div>
    </div>
  );
}

function UsageBar({ label, used, limit }: { label: string; used: number; limit: number }) {
  const pct = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
  const color = pct >= 100 ? 'bg-danger' : pct >= 80 ? 'bg-warning-border' : 'bg-primary';

  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">{used.toLocaleString()} / {limit.toLocaleString()}</span>
      </div>
      <div className="h-2 bg-panel rounded-[--radius-full] overflow-hidden">
        <div className={`h-full ${color} rounded-[--radius-full] transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
