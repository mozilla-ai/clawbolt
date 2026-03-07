import { useState, useEffect } from 'react';
import { useOutletContext, Link } from 'react-router-dom';
import Card from '@/components/ui/card';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import type { ContractorStats } from '@/types';
import type { AppShellContext } from '@/layouts/AppShell';

export default function OverviewPage() {
  const { profile } = useOutletContext<AppShellContext>();
  const [stats, setStats] = useState<ContractorStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api.getStats()
      .then(setStats)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold">
          {profile?.name ? `Welcome back, ${profile.name}` : 'Dashboard'}
        </h2>
        <p className="text-sm text-muted-foreground mt-1">
          Overview of your AI assistant activity.
        </p>
      </div>

      {loading ? (
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      ) : error ? (
        <Card className="text-center py-8">
          <p className="text-sm text-danger">{error}</p>
          <button
            className="text-sm text-primary hover:underline mt-2"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </Card>
      ) : stats ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Conversations"
            value={stats.total_sessions}
            linkTo="/app/conversations"
          />
          <StatCard
            label="Messages this month"
            value={stats.messages_this_month}
          />
          <StatCard
            label="Memory facts"
            value={stats.total_memory_facts}
            linkTo="/app/memory"
          />
          <StatCard
            label="Checklist items"
            value={stats.active_checklist_items}
            linkTo="/app/checklist"
          />
        </div>
      ) : null}

      {stats?.last_conversation_at && (
        <p className="text-xs text-muted-foreground mt-4">
          Last conversation: {new Date(stats.last_conversation_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

function StatCard({ label, value, linkTo }: { label: string; value: number; linkTo?: string }) {
  const content = (
    <Card className={linkTo ? 'hover:border-primary/50 transition-colors cursor-pointer' : ''}>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold mt-1">{value}</p>
    </Card>
  );

  if (linkTo) {
    return <Link to={linkTo}>{content}</Link>;
  }
  return content;
}
