import { useState, useEffect, useCallback } from 'react';
import { toast } from '@/lib/toast';
import {
  getReportedConversations,
  getReportedConversationMessages,
  dismissReportedConversation,
  type ReportedConversation,
  type ReportedConversationMessageList,
  type ReportedStatus,
} from '../admin-api';
import ConfirmDialog from '../ConfirmDialog';
import Pagination from '../components/Pagination';
import { formatAbsolute, formatRelative } from '../format';

// ---------------------------------------------------------------------------
// Reported conversations tab — admin queue of /report submissions.
// ---------------------------------------------------------------------------
//
// Two views in one component:
//
// 1. The list view: paginated queue with status filter (All / Open / Dismissed).
// 2. The detail view: messages around the report's anchor seq + Dismiss action.
//
// State is intentionally local. ``selectedReport`` toggles between views; we
// don't push a hash deep-link for now because reports are short-lived (open
// → triage → dismiss) and the list reload is cheap. Add hash routing if the
// queue grows past a few hundred entries.

const PAGE_SIZE = 50;

interface ReportedTabProps {
  /** Pushes the latest open count up to the parent so a caller can badge
   * the queue. Not consumed today; the prop keeps the wiring a one-liner. */
  onOpenCountChange?: (count: number) => void;
  /** Navigate to a user's detail page (``/app/admin/users/{id}``). */
  onSelectUserById?: (userId: string) => void;
}

export default function ReportedTab({
  onOpenCountChange,
  onSelectUserById,
}: ReportedTabProps = {}) {
  const [items, setItems] = useState<ReportedConversation[]>([]);
  const [total, setTotal] = useState(0);
  const [openCount, setOpenCount] = useState(0);
  const [statusFilter, setStatusFilter] = useState<ReportedStatus | 'all'>('all');
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<ReportedConversation | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getReportedConversations({
      status: statusFilter === 'all' ? undefined : statusFilter,
      offset: page * PAGE_SIZE,
      limit: PAGE_SIZE,
    })
      .then(res => {
        setItems(res.items);
        setTotal(res.total);
        setOpenCount(res.open_count);
        onOpenCountChange?.(res.open_count);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [statusFilter, page, onOpenCountChange]);

  useEffect(() => {
    load();
  }, [load]);

  if (selected) {
    return (
      <ReportedDetailView
        report={selected}
        onBack={() => setSelected(null)}
        onSelectUserById={onSelectUserById}
        onDismissed={() => {
          setSelected(null);
          load();
        }}
      />
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center gap-3 flex-wrap">
        <p className="text-xs text-muted-foreground">
          User-initiated reports filed via <code>/report</code>. Open: {openCount}. Total: {total}.
        </p>
        <div className="ml-auto flex gap-1" role="tablist" aria-label="Report status filter">
          {(['all', 'open', 'dismissed'] as const).map(opt => (
            <button
              key={opt}
              type="button"
              role="tab"
              aria-selected={statusFilter === opt}
              className={`text-xs px-2.5 py-1 rounded-[--radius-sm] border ${
                statusFilter === opt
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'border-border text-muted-foreground hover:text-foreground'
              }`}
              onClick={() => {
                setStatusFilter(opt);
                setPage(0);
              }}
            >
              {opt === 'all' ? 'All' : opt === 'open' ? 'Open' : 'Dismissed'}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />
      ) : error ? (
        <div className="text-danger text-sm">
          {error}{' '}
          <button className="text-primary hover:underline" onClick={load} type="button">
            Retry
          </button>
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">No reports in this view.</p>
      ) : (
        <div className="bg-card border border-border rounded-[--radius-md] divide-y divide-border/50 mb-3">
          {items.map(r => (
            <button
              key={r.id}
              type="button"
              onClick={() => setSelected(r)}
              className="w-full text-left flex flex-col sm:flex-row sm:items-center gap-2 px-3 py-2.5 hover:bg-secondary-hover focus:outline-none focus:ring-2 focus:ring-primary"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm truncate">{r.user_email || r.user_id}</span>
                  <span
                    className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded ${
                      r.status === 'open'
                        ? 'bg-warning-bg text-warning'
                        : 'bg-panel text-muted-foreground'
                    }`}
                  >
                    {r.status}
                  </span>
                  {r.channel && (
                    <span className="text-[10px] text-muted-foreground">via {r.channel}</span>
                  )}
                </div>
                {r.reason && (
                  <div className="text-xs text-muted-foreground mt-0.5 truncate">
                    {r.reason}
                  </div>
                )}
              </div>
              <div className="text-xs text-muted-foreground shrink-0">
                <span title={formatAbsolute(r.created_at)}>{formatRelative(r.created_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}

      {total > PAGE_SIZE && (
        <Pagination page={page} total={total} pageSize={PAGE_SIZE} onChange={setPage} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail view: messages around anchor + Dismiss action.
// ---------------------------------------------------------------------------

function ReportedDetailView({
  report,
  onBack,
  onDismissed,
  onSelectUserById,
}: {
  report: ReportedConversation;
  onBack: () => void;
  onDismissed: () => void;
  onSelectUserById?: (userId: string) => void;
}) {
  const [data, setData] = useState<ReportedConversationMessageList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDismiss, setConfirmingDismiss] = useState(false);
  const [dismissBusy, setDismissBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getReportedConversationMessages(report.id, { window: 20 })
      .then(res => {
        if (!cancelled) setData(res);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [report.id]);

  const doDismiss = async () => {
    setDismissBusy(true);
    try {
      await dismissReportedConversation(report.id);
      toast.success('Report dismissed.');
      onDismissed();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDismissBusy(false);
      setConfirmingDismiss(false);
    }
  };

  return (
    <div>
      <div className="mb-3 flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={onBack}
          className="text-xs text-primary hover:underline"
        >
          ← Back to queue
        </button>
        <span className="text-muted-foreground text-xs">|</span>
        {onSelectUserById ? (
          <button
            type="button"
            // Navigation hand-off to the Users section, not a state pivot
            // inside Reported: the router owns /app/admin/users/{id}.
            onClick={() => onSelectUserById(report.user_id)}
            className="text-sm font-medium hover:underline decoration-dotted underline-offset-2"
            title="Open user record"
          >
            {report.user_email || report.user_id}
          </button>
        ) : (
          <span className="text-sm font-medium">{report.user_email || report.user_id}</span>
        )}
        <span
          className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded ${
            report.status === 'open'
              ? 'bg-warning-bg text-warning'
              : 'bg-panel text-muted-foreground'
          }`}
        >
          {report.status}
        </span>
        {report.channel && (
          <span className="text-xs text-muted-foreground">via {report.channel}</span>
        )}
        <span
          className="text-xs text-muted-foreground"
          title={formatAbsolute(report.created_at)}
        >
          {formatRelative(report.created_at)}
        </span>
        {report.status === 'open' && (
          <button
            type="button"
            onClick={() => setConfirmingDismiss(true)}
            className="ml-auto px-3 py-1.5 text-xs font-medium rounded-[--radius-sm] border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50"
            disabled={dismissBusy}
          >
            Dismiss
          </button>
        )}
        {report.status === 'dismissed' && report.reviewed_admin_email && (
          <span className="ml-auto text-xs text-muted-foreground">
            Closed by {report.reviewed_admin_email}
          </span>
        )}
      </div>

      {report.reason && (
        <div className="mb-3 text-sm bg-panel border border-border rounded-[--radius-md] p-3">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Reason
          </span>
          <p className="mt-1">{report.reason}</p>
        </div>
      )}

      <h3 className="text-sm font-semibold mb-2">
        Conversation around the report
      </h3>
      {loading ? (
        <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />
      ) : error ? (
        <div className="text-danger text-sm">{error}</div>
      ) : !data || data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          No messages available. The conversation may have been deleted.
        </p>
      ) : (
        <ul className="bg-card border border-border rounded-[--radius-md] divide-y divide-border/50">
          {data.items.map(m => (
            <li
              key={m.seq}
              className={`px-3 py-2 text-sm ${
                m.is_anchor ? 'bg-primary-light/20 border-l-2 border-l-primary' : ''
              }`}
            >
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                <span className="uppercase">{m.direction}</span>
                <span>· seq {m.seq}</span>
                {m.timestamp && (
                  <span title={formatAbsolute(m.timestamp)}>
                    · {formatRelative(m.timestamp)}
                  </span>
                )}
                {m.is_anchor && (
                  <span className="text-primary font-medium">(anchor)</span>
                )}
              </div>
              <div className="mt-1 whitespace-pre-wrap break-words">{m.body || <em className="text-muted-foreground">(empty)</em>}</div>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={confirmingDismiss}
        onClose={() => setConfirmingDismiss(false)}
        onConfirm={doDismiss}
        title="Dismiss this report?"
        description={
          <p>
            Marks the report as dismissed and stamps your admin id on it. The
            user is not notified. The report remains visible under the
            "Dismissed" filter for forensic reference.
          </p>
        }
        confirmLabel="Dismiss"
        busy={dismissBusy}
      />
    </div>
  );
}

