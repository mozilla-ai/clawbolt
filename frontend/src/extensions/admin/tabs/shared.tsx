import {
  useId,
  useState,
  useEffect,
  useMemo,
  useRef,
  createContext,
  useContext,
  type ReactNode,
} from 'react';
import {
  getSharedDataConversationTurns,
  getSharedDataProfile,
  getSharedDataHeartbeatLogs,
  getSharedDataMemory,
  getSharedDataCompactionEvents,
  getSharedDataApprovalEvents,
  type SharedDataUser,
  type SharedDataConversationTurns,
  type SharedDataTurn,
  type SharedDataMessage,
  type SharedDataToolCall,
  type SharedDataProfile,
  type SharedDataHeartbeatLogList,
  type SharedDataMemoryDocument,
  type SharedDataCompactionEvent,
  type SharedDataCompactionEventList,
  type SharedDataCompactionSnapshot,
  type SharedDataApprovalEventList,
} from '../admin-api';
import { toBlob } from 'html-to-image';
import ActivityFilterBar from '../components/ActivityFilterBar';
import { formatAbsolute, formatAbsoluteWithSeconds, formatRelative } from '../format';
import { toast } from '@/lib/toast';

// ---------------------------------------------------------------------------
// Consent-gated per-user views, exported for embedding in the user-detail
// surface (../tabs/user-detail.tsx). The standalone Shared admin tab that
// used to wrap these was removed in #358; the per-user pieces live on as
// the Activity / Memory / Profile sub-tabs of user-detail.
// ---------------------------------------------------------------------------

export function SharedProfileView({ user }: { user: SharedDataUser }) {
  const [data, setData] = useState<SharedDataProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getSharedDataProfile(user.id)
      .then(res => !cancelled && setData(res))
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [user.id]);

  if (loading) return <div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />;
  if (error) return <div className="text-danger text-sm">{error}</div>;
  if (!data) return null;
  return (
    <div className="space-y-3">
      <ProfileTextSection title="Soul" body={data.soul_text} hint="How the agent should behave for this user." />
      <ProfileTextSection title="User profile" body={data.user_text} hint="Synthesized profile the agent has built up." />
      <ProfileTextSection title="Heartbeat directives" body={data.heartbeat_text} hint="Standing instructions for proactive messages." />
      <div className="bg-card border border-border rounded-[--radius-md] p-3">
        <div className="text-xs font-medium mb-1">Heartbeat config</div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Opt-in</dt>
          <dd>{data.heartbeat_opt_in ? 'yes' : 'no'}</dd>
          <dt className="text-muted-foreground">Frequency</dt>
          <dd className="font-mono">{data.heartbeat_frequency}</dd>
          <dt className="text-muted-foreground">Max per day</dt>
          <dd>{data.heartbeat_max_daily}</dd>
        </dl>
      </div>
    </div>
  );
}

function ProfileTextSection({
  title,
  body,
  hint,
}: {
  title: string;
  body: string;
  hint: string;
}) {
  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3">
      <div className="text-xs font-medium">{title}</div>
      <div className="text-[10px] text-muted-foreground mb-1">{hint}</div>
      {body ? (
        <pre className="whitespace-pre-wrap break-words text-xs font-mono bg-panel rounded-[--radius-sm] p-2">
          {body}
        </pre>
      ) : (
        <p className="text-xs italic text-muted-foreground">(empty)</p>
      )}
    </div>
  );
}

export function SharedMemoryView({ user }: { user: SharedDataUser }) {
  const [data, setData] = useState<SharedDataMemoryDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getSharedDataMemory(user.id)
      .then(res => !cancelled && setData(res))
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [user.id]);

  if (loading) return <div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />;
  if (error) return <div className="text-danger text-sm">{error}</div>;
  if (!data) return null;
  const isEmpty = !data.memory_text && !data.history_text;
  return (
    <div className="space-y-3">
      {data.updated_at && (
        <div className="text-[10px] text-muted-foreground">
          Last updated {formatRelative(data.updated_at)}{' '}
          <span title={formatAbsolute(data.updated_at)}>·</span>
        </div>
      )}
      {isEmpty ? (
        <p className="text-sm text-muted-foreground italic">
          No memory document yet. The agent writes here as it learns and as
          sessions get compacted.
        </p>
      ) : (
        <>
          <ProfileTextSection
            title="Working memory"
            body={data.memory_text}
            hint="Agent's persistent notes for this user."
          />
          <ProfileTextSection
            title="Compacted history"
            body={data.history_text}
            hint="Accumulated facts extracted from older sessions during compaction."
          />
        </>
      )}
      <CompactionEventsSection user={user} />
    </div>
  );
}

function CompactionEventsSection({ user }: { user: SharedDataUser }) {
  const [data, setData] = useState<SharedDataCompactionEventList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getSharedDataCompactionEvents(user.id, { limit: 100 })
      .then(res => !cancelled && setData(res))
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [user.id]);

  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-3">
      <div className="text-xs font-medium">Compaction events</div>
      <div className="text-[10px] text-muted-foreground mb-2">
        Per-event metadata: when each compaction fired, how big the input was,
        what got updated. Content stays in Compacted history above.
      </div>
      {loading ? (
        <div className="animate-pulse h-24 bg-panel rounded-[--radius-sm]" />
      ) : error ? (
        <div className="text-danger text-xs">{error}</div>
      ) : !data || data.items.length === 0 ? (
        <p className="text-xs italic text-muted-foreground">
          No compaction events yet.
        </p>
      ) : (
        <ul className="divide-y divide-border/50">
          {data.items.map(event => (
            <CompactionEventRow key={event.id} event={event} />
          ))}
        </ul>
      )}
    </div>
  );
}

function CompactionEventRow({
  event,
}: {
  event: SharedDataCompactionEventList['items'][number];
}) {
  const [expanded, setExpanded] = useState(false);
  const updates: string[] = [];
  if (event.memory_updated) updates.push('memory');
  if (event.user_profile_updated) updates.push('user');
  if (event.soul_updated) updates.push('soul');
  const seqRange = formatSeqRange(event.min_message_seq, event.max_message_seq);
  return (
    <li className="py-1.5 text-[11px]">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="w-full text-left grid grid-cols-[auto_1fr_auto] gap-x-2 items-baseline hover:text-primary"
        aria-expanded={expanded}
      >
        <span className="font-mono text-muted-foreground">
          {event.triggered_at ? formatRelative(event.triggered_at) : '—'}
        </span>
        <span>
          {event.trimmed_count} msg / {event.trimmed_chars.toLocaleString()} chars
          {' · '}
          {event.duration_ms}ms
          {' · '}
          {event.input_tokens.toLocaleString()} in / {event.output_tokens.toLocaleString()} out
          {seqRange && (
            <>
              {' · '}
              <span className="font-mono text-muted-foreground">{seqRange}</span>
            </>
          )}
          {' · '}
          <CompactionStatusPill status={event.status} />
          {updates.length > 0 && (
            <>
              {' · updated '}
              <span className="font-medium">{updates.join(', ')}</span>
            </>
          )}
        </span>
        <span
          className="text-muted-foreground"
          title={event.triggered_at ? formatAbsolute(event.triggered_at) : ''}
        >
          #{event.id} {expanded ? '▾' : '▸'}
        </span>
      </button>
      {expanded && (
        <div className="mt-2 ml-2 border-l-2 border-border pl-3 space-y-3">
          <div>
            <div className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
              Memory file diffs
            </div>
            <CompactionSnapshotDiffs event={event} />
          </div>
          <div>
            <div className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
              LLM call
            </div>
            <CompactionLLMCall event={event} />
          </div>
        </div>
      )}
    </li>
  );
}

// Format ``[min, max]`` as a compact seq-range string, falling back when
// either bound is missing (legacy rows have no min_message_seq because
// the column was added in migration 030).
function formatSeqRange(min: number | null, max: number | null): string {
  if (min == null && max == null) return '';
  if (min == null) return `seq …–${max}`;
  if (max == null) return `seq ${min}–…`;
  if (min === max) return `seq ${min}`;
  return `seq ${min}–${max}`;
}

function CompactionStatusPill({ status }: { status: string }) {
  // 'pending' rows are either still in flight or were left behind by a
  // crashed async task. Either case is worth flagging visually so admins
  // know the snapshots may not be populated for that row.
  const cls =
    status === 'pending'
      ? 'bg-warning-border/30 text-warning-border'
      : 'bg-success-bg text-success';
  return (
    <span
      className={`uppercase text-[9px] font-medium px-1 py-px rounded-[--radius-sm] ${cls}`}
    >
      {status}
    </span>
  );
}

// Click-to-expand wrapper for compaction metadata sections (memory file
// diffs, LLM prompt, raw response, parsed fields). Defaults to collapsed
// so the activity feed stays scannable; admins click into the specific
// piece they want to inspect, mirroring the per-tool-call collapse
// pattern in ToolCallRow.
function CollapsibleSection({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="w-full text-left flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground hover:text-primary"
        aria-expanded={expanded}
      >
        <span>{label}</span>
        <span className="text-[10px]">{expanded ? '▾' : '▸'}</span>
      </button>
      {expanded && <div className="mt-1">{children}</div>}
    </div>
  );
}

// Two-pane diff for each of the four memory files this event touched.
// Skips files that are unchanged (both before and after empty/null) so a
// memory-only compaction shows just memory rows. Truncated snapshots
// render head/tail/size visibly so an admin can still see what was
// rewritten without storing the full body twice.
function CompactionSnapshotDiffs({
  event,
}: {
  event: SharedDataCompactionEvent;
}) {
  const files: Array<{
    label: string;
    before: SharedDataCompactionSnapshot;
    after: SharedDataCompactionSnapshot;
  }> = [
    { label: 'MEMORY.md', before: event.memory_text_before, after: event.memory_text_after },
    {
      label: 'HISTORY.md',
      before: event.history_text_before,
      after: event.history_text_after,
    },
    { label: 'USER.md', before: event.user_text_before, after: event.user_text_after },
    { label: 'SOUL.md', before: event.soul_text_before, after: event.soul_text_after },
  ];
  const populated = files.filter(
    f => snapshotHasContent(f.before) || snapshotHasContent(f.after),
  );
  if (populated.length === 0) {
    return (
      <p className="text-[11px] italic text-muted-foreground">
        No file diffs recorded for this event. (Pending rows fill this in
        once the async compaction call lands; legacy rows from before
        migration 030 will stay empty.)
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {populated.map(f => (
        <CollapsibleSection key={f.label} label={f.label}>
          <div className="grid grid-cols-2 gap-2">
            <SnapshotPane label="before" snap={f.before} />
            <SnapshotPane label="after" snap={f.after} />
          </div>
        </CollapsibleSection>
      ))}
    </div>
  );
}

function snapshotHasContent(snap: SharedDataCompactionSnapshot): boolean {
  return snap.text != null || snap.truncated;
}

// Renders the migration-031 LLM-call capture: trimmed conversation
// prompt the LLM saw, the unparsed response, and the parsed fields.
// "Why didn't this compaction touch USER.md?" is the load-bearing
// question; surfacing all four parsed fields, including empties,
// answers it directly without raw-SQL spelunking.
function CompactionLLMCall({ event }: { event: SharedDataCompactionEvent }) {
  if (event.status === 'pending') {
    return (
      <div className="bg-panel rounded-[--radius-sm] p-2 text-[11px] italic text-muted-foreground">
        Compaction LLM call still running (or crashed before completing).
        Prompt and response will populate when the async task lands and
        this row flips to "completed".
      </div>
    );
  }
  const anyContent =
    snapshotHasContent(event.prompt) ||
    snapshotHasContent(event.raw_response) ||
    snapshotHasContent(event.parsed_response);
  if (!anyContent) {
    // Legacy rows from before migration 031 stay empty. Surface the
    // explanation rather than rendering a stack of "(unchanged)" panes
    // that would be misleading.
    return (
      <div className="text-[11px] italic text-muted-foreground">
        No LLM-call capture for this event. (Rows predating migration 031.)
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <CollapsibleSection label="Prompt (trimmed conversation sent to LLM)">
        <SnapshotPane label="content" snap={event.prompt} />
      </CollapsibleSection>
      <CollapsibleSection label="Raw response">
        <SnapshotPane label="content" snap={event.raw_response} />
      </CollapsibleSection>
      <CollapsibleSection label="Parsed fields">
        <ParsedResponseFields snap={event.parsed_response} />
      </CollapsibleSection>
    </div>
  );
}

// Renders the parsed CompactionResult shape (memory_update, summary,
// user_profile_update, soul_update). Each field shows its content +
// char count, or an explicit "(empty, file unchanged)" placeholder for
// empties so an admin can see "the LLM returned nothing for soul_update"
// rather than just noticing the row missing.
function ParsedResponseFields({ snap }: { snap: SharedDataCompactionSnapshot }) {
  if (snap.truncated) {
    // The OSS truncation envelope means the parsed-fields JSON itself
    // was too long to store; surface the same head/tail UI rather than
    // pretending we can split it into four fields.
    return <SnapshotPane label="content" snap={snap} />;
  }
  if (snap.text == null) {
    return (
      <div className="bg-panel rounded-[--radius-sm] p-2 text-[11px] italic text-muted-foreground">
        (no parsed response captured)
      </div>
    );
  }
  let parsed: Record<string, unknown> | null = null;
  try {
    const candidate = JSON.parse(snap.text);
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      parsed = candidate as Record<string, unknown>;
    }
  } catch {
    // Fall through to the raw-text fallback below.
  }
  if (parsed === null) {
    return (
      <pre className="bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono whitespace-pre-wrap break-words">
        {snap.text}
      </pre>
    );
  }
  const fields: Array<{ key: string; label: string }> = [
    { key: 'memory_update', label: 'memory_update (MEMORY.md rewrite)' },
    { key: 'summary', label: 'summary (HISTORY.md append)' },
    { key: 'user_profile_update', label: 'user_profile_update (USER.md rewrite)' },
    { key: 'soul_update', label: 'soul_update (SOUL.md rewrite)' },
  ];
  return (
    <div className="space-y-2">
      {fields.map(f => {
        const value = parsed?.[f.key];
        const text = typeof value === 'string' ? value : '';
        const empty = text.length === 0;
        return (
          <div key={f.key}>
            <div className="text-[9px] uppercase text-muted-foreground">
              {f.label}
              {!empty && <span> · {text.length} chars</span>}
            </div>
            {empty ? (
              <div
                className="bg-panel rounded-[--radius-sm] p-2 text-[11px] italic text-muted-foreground"
                title="The LLM returned no update for this file. The file stays unchanged this event."
              >
                (empty, file unchanged)
              </div>
            ) : (
              <pre className="bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono whitespace-pre-wrap break-words">
                {text}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SnapshotPane({
  label,
  snap,
}: {
  // 'before' / 'after' for two-pane memory-file diffs; 'content' for
  // the migration-031 LLM-call panes where there's only one slice to
  // show (prompt / raw response / parsed fields).
  label: 'before' | 'after' | 'content';
  snap: SharedDataCompactionSnapshot;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase text-muted-foreground mb-0.5">{label}</div>
      {snap.truncated ? (
        <div className="bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono space-y-2">
          <div className="text-warning-border text-[10px]">
            truncated · {formatBytes(snap.size_bytes ?? 0)}
            {snap.sha256 && (
              <span className="text-muted-foreground">
                {' · sha256 '}
                <span title={snap.sha256}>{snap.sha256.slice(0, 8)}</span>
              </span>
            )}
          </div>
          {snap.head && (
            <div>
              <div className="text-[9px] uppercase text-muted-foreground">head</div>
              <pre className="whitespace-pre-wrap break-words">{snap.head}</pre>
            </div>
          )}
          {snap.tail && (
            <div>
              <div className="text-[9px] uppercase text-muted-foreground">tail</div>
              <pre className="whitespace-pre-wrap break-words">{snap.tail}</pre>
            </div>
          )}
        </div>
      ) : snap.text != null ? (
        <pre className="bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono whitespace-pre-wrap break-words">
          {snap.text || <span className="italic text-muted-foreground">(empty)</span>}
        </pre>
      ) : (
        <div className="bg-panel rounded-[--radius-sm] p-2 text-[11px] italic text-muted-foreground">
          (unchanged)
        </div>
      )}
    </div>
  );
}

// Compact byte count for the truncation banner. Uses the same kibi/mebi
// shorthand admins are used to from log dashboards so a 250_000 byte
// snapshot reads as "244 KB" rather than "250000 B".
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Collapsed-by-default disclosure for an agent reply's extended-thinking
// text (the LLM's reasoning, captured by OSS migration 033). Mirrors the
// chevron + aria-expanded pattern used by ToolCallRow so admins recognize
// the affordance immediately. Lives inside the agent-reply detail block,
// above the tool-call list, so a long reply still scans body-first.
// Controlled disclosure: expansion state is lifted to SharedActivityView so
// the Share snippet can mirror exactly what the admin has open (issue #607).
function ReasoningRow({
  text,
  expanded,
  onToggle,
}: {
  text: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const panelId = useId();
  return (
    <div className="text-xs">
      <button
        type="button"
        onClick={onToggle}
        className="text-left flex items-center gap-2 hover:text-primary"
        aria-expanded={expanded}
        aria-controls={panelId}
      >
        <span className="text-muted-foreground uppercase">Reasoning</span>
        <span className="text-muted-foreground text-[10px]">
          {expanded ? '▾' : '▸'}
        </span>
      </button>
      {expanded && (
        <pre
          id={panelId}
          className="mt-1 ml-4 whitespace-pre-wrap break-words bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono"
        >
          {text}
        </pre>
      )}
    </div>
  );
}

function ToolCallRow({
  call,
  expanded,
  onToggle,
}: {
  call: SharedDataToolCall;
  expanded: boolean;
  onToggle: () => void;
}) {
  const panelId = useId();
  const hasArgs = Object.keys(call.args).length > 0;
  const hasResult = call.result.length > 0;
  const canExpand = hasArgs || hasResult || call.receipt !== null;
  return (
    <li className="text-xs">
      <button
        type="button"
        onClick={() => canExpand && onToggle()}
        className={`w-full text-left flex items-center gap-2 ${canExpand ? 'hover:text-primary' : 'cursor-default'}`}
        aria-expanded={canExpand ? expanded : undefined}
        aria-controls={canExpand ? panelId : undefined}
      >
        <span
          className={`font-mono ${call.is_error ? 'text-danger' : 'text-foreground'}`}
        >
          {call.is_error ? '✗' : '✓'} {call.name}
        </span>
        {canExpand && (
          <span className="text-muted-foreground text-[10px]">
            {expanded ? '▾' : '▸'}
          </span>
        )}
      </button>
      {expanded && (
        <div id={panelId} className="mt-1 ml-4 space-y-1">
          {hasArgs && (
            <div>
              <div className="text-[10px] text-muted-foreground uppercase">args</div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono">
                {JSON.stringify(call.args, null, 2)}
              </pre>
            </div>
          )}
          {hasResult && (
            <div>
              <div className="text-[10px] text-muted-foreground uppercase">result</div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono">
                {call.result}
              </pre>
            </div>
          )}
          {call.receipt && (
            <div>
              <div className="text-[10px] text-muted-foreground uppercase">receipt</div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words bg-panel rounded-[--radius-sm] p-2 text-[11px] font-mono">
                {JSON.stringify(call.receipt, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Activity timeline: unified, filterable, newest-first view that merges
// conversation turns / heartbeats / compactions for one consenting user.
//
// Each turn surfaces as up to two rows (one user-message, one agent-reply)
// so admins can scan inbound traffic and outbound replies inline with
// system events instead of seeing the whole conversation as a single row.
// Message bodies and tool calls render inline on every row (the merged
// view that replaced the standalone Conversation sub-tab in #404), so
// admins do not need to click into each row to read what was said.
// Tool call args / result / receipt stay click-to-expand inside their
// own row to keep noisy payloads collapsed by default.
//
// Industry-standard log-viewer pattern (Datadog / Sentry / Grafana logs):
//   - top control bar: date range, type filter, free-text search, errors-only
//   - main: scrollable list of typed events newest-first
//   - per-row: timestamp · type pill · channel · full body / payload
//
// Implementation notes:
// - Data sources are existing per-type endpoints, fetched in parallel.
// - Date range filter goes server-side for heartbeats / compactions; the
//   turn endpoint is unfiltered (one conversation per user, the limit
//   bounds the size), so we apply the window client-side when merging.
// - Type filter, text search, and errors-only happen client-side.
// - At pilot scale (one consenting user, hundreds of events) plain
//   useMemo sort/filter is plenty. If we hit thousands per user we layer
//   in TanStack Table + virtualization without changing the contract.
// ---------------------------------------------------------------------------

type ActivityType =
  | 'user-message'
  | 'agent-reply'
  | 'heartbeat'
  | 'compaction'
  | 'approval';

interface ActivityItem {
  key: string; // unique React key
  type: ActivityType;
  timestamp: Date;
  timestampIso: string; // pre-formatted for display
  channel: string;
  summary: string;
  isError: boolean;
  // True for user-message / agent-reply rows whose seq is at or below
  // the session's ``last_trim_seq``. Trimmed messages still ride through
  // the response (they exist in the database) but the agent no longer
  // sees them on the next inbound, so the UI greys them out and tags
  // them so admins can tell live context from dropped backfill at a
  // glance.
  trimmed: boolean;
  // Original record kept around so the detail panel can render the full
  // typed payload without a second fetch.
  raw:
    | { kind: 'user-message'; turn: SharedDataTurn; message: SharedDataMessage }
    | { kind: 'agent-reply'; turn: SharedDataTurn; message: SharedDataMessage }
    | { kind: 'heartbeat'; data: SharedDataHeartbeatLogList['items'][number] }
    | { kind: 'compaction'; data: SharedDataCompactionEventList['items'][number] }
    // Approval rows can carry either a single event (a still-pending
    // request, or an orphan resolution whose request fell outside the
    // window) or a paired request + resolution. The OSS approval gate
    // allows only one pending approval per user at a time, so any
    // 'requested' row immediately followed by a 'decided' / 'timed_out'
    // / 'recovered' row for the same tool is its resolution, and the
    // pair is rendered as one card to keep the activity feed compact
    // (issue #507). At least one of request / resolution is non-null.
    | {
        kind: 'approval';
        request: SharedDataApprovalEventList['items'][number] | null;
        resolution: SharedDataApprovalEventList['items'][number] | null;
      };
}

const ACTIVITY_TYPES: readonly ActivityType[] = [
  'user-message',
  'agent-reply',
  'heartbeat',
  'compaction',
  'approval',
];

const TYPE_LABELS: Record<ActivityType, string> = {
  'user-message': 'User',
  'agent-reply': 'Agent',
  heartbeat: 'Heartbeat',
  compaction: 'Compaction',
  approval: 'Approval',
};

const TYPE_PILL_CLASSES: Record<ActivityType, string> = {
  'user-message': 'bg-primary/15 text-primary',
  'agent-reply': 'bg-success-bg text-success',
  heartbeat: 'bg-warning-border/20 text-warning-border',
  compaction: 'bg-secondary-hover text-foreground',
  approval: 'bg-info-bg text-info',
};

// Lowercase and collapse runs of non-alphanumeric characters to a single
// space. This lets queries with spaces or punctuation match identifiers
// that use other separators: "company cam" becomes "company cam" and
// matches "companycam_search_projects" (which becomes "companycam search
// projects") because both tokens "company" and "cam" appear as substrings.
function normalize(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, ' ');
}

// Build a single searchable string per activity row covering everything
// admins reasonably expect to find: the rendered summary and channel, plus
// per-row payload fields (full message bodies, every tool call's name /
// args / result / receipt, heartbeat reasoning, etc.). The matcher then
// runs token containment over this haystack, so no field-by-field branching
// or per-tool hardcoding is needed.
function buildHaystack(item: ActivityItem): string {
  const parts: string[] = [item.summary, item.channel];
  switch (item.raw.kind) {
    case 'user-message':
      parts.push(item.raw.message.body);
      break;
    case 'agent-reply':
      parts.push(item.raw.message.body, item.raw.message.thinking);
      for (const c of item.raw.turn.tool_calls) {
        parts.push(c.name, JSON.stringify(c.args), c.result);
        if (c.receipt) {
          parts.push(c.receipt.action, c.receipt.target, c.receipt.url ?? '');
        }
      }
      break;
    case 'heartbeat':
      parts.push(
        item.raw.data.message_text,
        item.raw.data.reasoning,
        item.raw.data.action_type,
        item.raw.data.tasks,
      );
      break;
    case 'compaction':
      // The summary already encodes the only human-readable content.
      break;
    case 'approval':
      for (const ev of [item.raw.request, item.raw.resolution]) {
        if (!ev) continue;
        parts.push(ev.event_type, ev.tool_name, ev.description, ev.decision ?? '');
      }
      break;
  }
  return normalize(parts.join(' '));
}

/** Local-date YYYY-MM-DD for use as the default activity window. Local
 * (not UTC) so it matches the date picker and the timestamps the rest of
 * the admin UI renders via `toLocaleString`. */
function todayLocalDate(): string {
  const d = new Date();
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Expansion state for the feed's collapsible bits (per-tool-call payloads,
// per-reply reasoning) is lifted out of the row components and shared via
// context so the Share snippet can mirror exactly what the admin has open
// (issue #607). Keys: reasoning is keyed by the row's ``item.key``; each
// tool call by ``${item.key}#${index}``.
interface ActivityExpansion {
  expandedTools: Set<string>;
  expandedReasoning: Set<string>;
  toggleTool: (key: string) => void;
  toggleReasoning: (key: string) => void;
}

const ExpansionContext = createContext<ActivityExpansion | null>(null);

function useExpansion(): ActivityExpansion {
  const ctx = useContext(ExpansionContext);
  if (!ctx) throw new Error('ActivityRow rendered outside ExpansionContext');
  return ctx;
}

function toggleInSet(prev: Set<string>, key: string): Set<string> {
  const next = new Set(prev);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

// Stable key for one tool call within a row, used both to track expansion
// in the feed and to mirror it in the snippet.
function toolCallKey(rowKey: string, index: number): string {
  return `${rowKey}#${index}`;
}

export function SharedActivityView({
  user,
}: {
  user: SharedDataUser;
}) {
  // Default to "today" so the feed loads with the most recent activity
  // instead of every event ever recorded for this user. Admins can widen
  // via the date inputs or the Clear range button in the filter bar.
  const [startDate, setStartDate] = useState<string>(() => todayLocalDate());
  const [endDate, setEndDate] = useState<string>(() => todayLocalDate());
  const [enabledTypes, setEnabledTypes] = useState<Set<ActivityType>>(
    new Set(ACTIVITY_TYPES),
  );
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [search, setSearch] = useState('');
  // Feed order. `merged` is always built newest-first; 'oldest' just reverses
  // the filtered result, so the toggle is a cheap array flip rather than a
  // recompute of the merge/explode work.
  const [sortDirection, setSortDirection] = useState<'newest' | 'oldest'>(
    'newest',
  );
  // Keys of rows the admin has highlighted to share. Selection persists
  // across filter changes; rows that drop out of the merged stream are
  // simply not included when building the snippet (see selectedItems).
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [shareOpen, setShareOpen] = useState(false);
  // Lifted expand/collapse state for tool-call payloads and reply reasoning,
  // so the Share snippet mirrors what is visible in the feed (issue #607).
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [expandedReasoning, setExpandedReasoning] = useState<Set<string>>(
    new Set(),
  );
  // Bumped by the Refresh button to force the fetches to re-run without
  // requiring a full page reload. Filter changes already trigger refetch
  // via their own deps; this exists for "I want to see what just landed
  // on the server" without touching dates / search / type chips.
  const [refreshKey, setRefreshKey] = useState(0);

  // Three independent fetches in parallel. We deliberately do NOT
  // block on all-three-resolve before showing anything: the list
  // flashes in source-by-source as each completes, which feels faster.
  // The turn-grouped endpoint pulls the full single conversation; we
  // explode each turn into one user-message and one agent-reply row
  // when building the merged stream below.
  const [turns, setTurns] = useState<SharedDataConversationTurns | null>(null);
  const [heartbeats, setHeartbeats] = useState<
    SharedDataHeartbeatLogList['items'] | null
  >(null);
  const [compactions, setCompactions] = useState<
    SharedDataCompactionEventList['items'] | null
  >(null);
  const [approvals, setApprovals] = useState<
    SharedDataApprovalEventList['items'] | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setTurns(null);
    setHeartbeats(null);
    setCompactions(null);
    setApprovals(null);

    const params: { start_date?: string; end_date?: string } = {};
    if (startDate) params.start_date = startDate;
    if (endDate) params.end_date = endDate;

    // The turns endpoint has no date filter (one conversation per user,
    // a small bounded set). Apply the window client-side when merging
    // so the activity list stays consistent with heartbeats / compactions.
    getSharedDataConversationTurns(user.id, { limit: 500 })
      .then(res => !cancelled && setTurns(res))
      .catch((e: Error) => !cancelled && setError(e.message));

    getSharedDataHeartbeatLogs(user.id, { ...params, limit: 200 })
      .then(res => !cancelled && setHeartbeats(res.items))
      .catch((e: Error) => !cancelled && setError(e.message));

    getSharedDataCompactionEvents(user.id, { ...params, limit: 200 })
      .then(res => !cancelled && setCompactions(res.items))
      .catch((e: Error) => !cancelled && setError(e.message));

    getSharedDataApprovalEvents(user.id, { ...params, limit: 500 })
      .then(res => !cancelled && setApprovals(res.items))
      .catch((e: Error) => !cancelled && setError(e.message));

    return () => {
      cancelled = true;
    };
  }, [user.id, startDate, endDate, refreshKey]);

  const merged = useMemo<ActivityItem[]>(() => {
    const out: ActivityItem[] = [];
    // Date strings (`YYYY-MM-DD`) come from local-time inputs but are
    // compared here against UTC timestamps. The end-of-day suffix is `Z`
    // so the bounds are UTC, matching how the backend interprets the
    // same `start_date` / `end_date` query params. Admins not in UTC may
    // see the window snap to UTC midnight rather than local midnight.
    const within = (ts: string | null | undefined): ts is string => {
      if (!ts) return false;
      if (startDate && ts < startDate) return false;
      if (endDate && ts > `${endDate}T23:59:59.999Z`) return false;
      return true;
    };
    const trimSeq = turns?.last_trim_seq ?? null;
    const isTrimmed = (seq: number): boolean => trimSeq != null && seq <= trimSeq;
    for (const turn of turns?.turns ?? []) {
      if (turn.user_message) {
        const ts = turn.user_message.timestamp ?? turn.started_at;
        if (within(ts)) {
          out.push({
            key: `tu:${turn.user_message.seq}`,
            type: 'user-message',
            timestamp: new Date(ts),
            timestampIso: ts,
            channel: 'user',
            summary: turn.user_message.body || '(empty)',
            isError: false,
            trimmed: isTrimmed(turn.user_message.seq),
            raw: { kind: 'user-message', turn, message: turn.user_message },
          });
        }
      }
      if (turn.agent_reply) {
        const ts = turn.agent_reply.timestamp ?? turn.finished_at;
        if (within(ts)) {
          const errorCount = turn.tool_calls.filter(c => c.is_error).length;
          const summary =
            turn.agent_reply.body ||
            (turn.tool_calls.length > 0
              ? `${turn.tool_calls.length} tool call${turn.tool_calls.length === 1 ? '' : 's'}`
              : '(empty)');
          out.push({
            key: `ta:${turn.agent_reply.seq}`,
            type: 'agent-reply',
            timestamp: new Date(ts),
            timestampIso: ts,
            channel: 'agent',
            summary,
            isError: errorCount > 0,
            trimmed: isTrimmed(turn.agent_reply.seq),
            raw: { kind: 'agent-reply', turn, message: turn.agent_reply },
          });
        }
      }
    }
    for (const h of heartbeats ?? []) {
      const ts = h.created_at;
      if (!ts) continue;
      out.push({
        key: `hb:${h.id}`,
        type: 'heartbeat',
        timestamp: new Date(ts),
        timestampIso: ts,
        channel: h.channel || 'system',
        summary: h.message_text || h.reasoning || `(${h.action_type})`,
        isError: h.action_type === 'error',
        trimmed: false,
        raw: { kind: 'heartbeat', data: h },
      });
    }
    for (const e of compactions ?? []) {
      const ts = e.triggered_at;
      if (!ts) continue;
      const updates = [
        e.memory_updated && 'memory',
        e.user_profile_updated && 'user',
        e.soul_updated && 'soul',
      ].filter(Boolean) as string[];
      out.push({
        key: `cp:${e.id}`,
        type: 'compaction',
        timestamp: new Date(ts),
        timestampIso: ts,
        channel: 'agent',
        summary:
          `${e.trimmed_count} msg / ${e.trimmed_chars.toLocaleString()} chars · ` +
          `${e.duration_ms}ms` +
          (updates.length > 0 ? ` · updated ${updates.join(', ')}` : ''),
        isError: false,
        trimmed: false,
        raw: { kind: 'compaction', data: e },
      });
    }
    // Pair each 'requested' approval with its immediately following
    // resolution ('decided' / 'timed_out' / 'recovered') for the same
    // tool, so the activity feed shows one row per approval lifecycle
    // instead of two stacked cards (issue #507). The OSS approval gate
    // enforces a single in-flight approval per user, so adjacency in
    // the chronologically sorted stream is sufficient to pair them;
    // see ``approval.py`` for the ordering guarantee.
    // Approvals are already filtered by start_date/end_date server-side
    // (see fetch above), matching how heartbeats and compactions are
    // handled. Only the turns stream needs client-side ``within`` filtering.
    const sortedApprovals = [...(approvals ?? [])]
      .filter(a => a.created_at != null)
      .sort((x, y) => {
        const tx = x.created_at!;
        const ty = y.created_at!;
        if (tx !== ty) return tx < ty ? -1 : 1;
        return x.id - y.id;
      });
    type ApprovalEvent = (typeof sortedApprovals)[number];
    const isResolution = (ev: ApprovalEvent): boolean =>
      ev.event_type === 'decided' ||
      ev.event_type === 'timed_out' ||
      ev.event_type === 'recovered';
    let i = 0;
    while (i < sortedApprovals.length) {
      const a = sortedApprovals[i]!;
      const next = sortedApprovals[i + 1] ?? null;
      const paired =
        a.event_type === 'requested' &&
        next != null &&
        next.tool_name === a.tool_name &&
        isResolution(next);
      // Any non-'requested' event is treated as a resolution row so an
      // unknown future event_type still gets rendered (matching the
      // original "every event gets a row" behavior). The summary /
      // detail fall through to the raw event_type for unknown values.
      const request = a.event_type === 'requested' ? a : null;
      const resolution = paired ? next! : request ? null : a;
      const display = resolution ?? request!;
      const ts = display.created_at!;
      // timed_out is the only branch that flags an error; decided rows
      // carry the user's decision in the summary so admins can scan the
      // approve/deny mix at a glance without opening the detail panel.
      const summary = !resolution
        ? `requested ${request!.tool_name}`
        : resolution.event_type === 'decided'
          ? `${resolution.tool_name} → ${resolution.decision ?? 'decided'}`
          : resolution.event_type === 'timed_out'
            ? `${resolution.tool_name} timed out`
            : resolution.event_type === 'recovered'
              ? `${resolution.tool_name} recovered after restart`
              : `${resolution.tool_name} ${resolution.event_type}`;
      out.push({
        key: paired
          ? `ap:${request!.id}-${resolution!.id}`
          : `ap:${display.id}`,
        type: 'approval',
        timestamp: new Date(ts),
        timestampIso: ts,
        channel: (request ?? resolution!).channel || 'system',
        summary,
        isError: resolution?.event_type === 'timed_out',
        trimmed: false,
        raw: { kind: 'approval', request, resolution },
      });
      i += paired ? 2 : 1;
    }
    out.sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());
    return out;
  }, [turns, heartbeats, compactions, approvals, startDate, endDate]);

  // Cache one haystack per row, recomputed only when the underlying data
  // changes. Keystrokes in the search box re-run only the cheap token
  // containment check below.
  const haystacks = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of merged) map.set(item.key, buildHaystack(item));
    return map;
  }, [merged]);

  const filtered = useMemo(() => {
    const tokens = normalize(search).split(' ').filter(Boolean);
    const rows = merged.filter(item => {
      if (!enabledTypes.has(item.type)) return false;
      if (errorsOnly && !item.isError) return false;
      if (tokens.length === 0) return true;
      const hay = haystacks.get(item.key) ?? '';
      return tokens.every(t => hay.includes(t));
    });
    // `merged` is newest-first; reversing the filtered subset (which preserves
    // that order) yields oldest-first without touching the merge memo.
    return sortDirection === 'oldest' ? rows.reverse() : rows;
  }, [merged, haystacks, enabledTypes, errorsOnly, search, sortDirection]);

  // Resolve the highlighted keys back to live rows for the share snippet.
  // Filtering against ``merged`` (not ``filtered``) keeps a selection alive
  // even if the admin narrows the type/search filters after picking rows.
  const selectedItems = useMemo(
    () => merged.filter(item => selected.has(item.key)),
    [merged, selected],
  );

  const toggleSelect = (key: string): void =>
    setSelected(prev => toggleInSet(prev, key));
  const expansion: ActivityExpansion = {
    expandedTools,
    expandedReasoning,
    toggleTool: (key: string) => setExpandedTools(prev => toggleInSet(prev, key)),
    toggleReasoning: (key: string) =>
      setExpandedReasoning(prev => toggleInSet(prev, key)),
  };

  // null sentinel = still in-flight for that source. Skeleton shows
  // until at least one stream resolves; a small "loading remaining
  // sources" hint takes over once any stream has data.
  const isLoading =
    turns === null && heartbeats === null && compactions === null && approvals === null;
  const partiallyLoaded =
    !isLoading &&
    (turns === null || heartbeats === null || compactions === null || approvals === null);

  return (
    <ExpansionContext.Provider value={expansion}>
      <ActivityFilterBar
        startDate={startDate}
        endDate={endDate}
        onStartDateChange={setStartDate}
        onEndDateChange={setEndDate}
        types={ACTIVITY_TYPES}
        typeLabels={TYPE_LABELS}
        enabledTypes={enabledTypes}
        onToggleType={t =>
          setEnabledTypes(prev => {
            const next = new Set(prev);
            if (next.has(t)) next.delete(t);
            else next.add(t);
            return next;
          })
        }
        errorsOnly={errorsOnly}
        onErrorsOnlyChange={setErrorsOnly}
        search={search}
        onSearchChange={setSearch}
        resultCount={filtered.length}
        totalCount={merged.length}
        onRefresh={() => setRefreshKey(k => k + 1)}
        isRefreshing={isLoading || partiallyLoaded}
        sortDirection={sortDirection}
        onToggleSortDirection={() =>
          setSortDirection(d => (d === 'newest' ? 'oldest' : 'newest'))
        }
      />

      {error && <div className="text-danger text-xs mb-2">{error}</div>}

      {selectedItems.length > 0 && (
        <div className="flex items-center gap-3 mb-2 bg-card border border-border rounded-[--radius-md] px-3 py-2 text-xs">
          <span className="font-medium">
            {selectedItems.length} highlighted
          </span>
          <button
            type="button"
            onClick={() => setShareOpen(true)}
            className="px-2.5 py-1 rounded-[--radius-md] bg-primary text-primary-foreground font-medium hover:bg-primary-hover"
          >
            Share snippet
          </button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="text-primary hover:underline"
          >
            Clear
          </button>
        </div>
      )}

      {isLoading ? (
        <div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />
      ) : filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          {merged.length === 0
            ? 'No activity in this range yet.'
            : 'No events match these filters.'}
        </p>
      ) : (
        <ul className="space-y-2">
          {filtered.map(item => (
            <ActivityRow
              key={item.key}
              item={item}
              selected={selected.has(item.key)}
              onToggleSelect={() => toggleSelect(item.key)}
            />
          ))}
        </ul>
      )}

      {shareOpen && selectedItems.length > 0 && (
        <ShareSnippetDialog
          items={selectedItems}
          expandedTools={expandedTools}
          expandedReasoning={expandedReasoning}
          onClose={() => setShareOpen(false)}
        />
      )}
      {partiallyLoaded && (
        <div className="text-[10px] text-muted-foreground mt-2 italic">
          Loading remaining sources...
        </div>
      )}
    </ExpansionContext.Provider>
  );
}

function ActivityRow({
  item,
  selected,
  onToggleSelect,
}: {
  item: ActivityItem;
  selected: boolean;
  onToggleSelect: () => void;
}) {
  // Trimmed rows are still in the database but the agent no longer
  // sees them. We grey the body and tag the row so admins can tell at
  // a glance which messages are live LLM context vs. historical
  // backfill the trim path has dropped.
  const trimmedCls = item.trimmed ? 'opacity-60' : '';
  // Highlighted rows get a ring so the admin can see what the Share
  // snippet will include while they scan the feed.
  const selectedCls = selected ? 'ring-2 ring-primary' : '';
  return (
    <li
      className={`bg-card border border-border rounded-[--radius-md] p-3 ${trimmedCls} ${selectedCls}`}
    >
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground mb-2 flex-wrap">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          aria-label="Highlight for sharing"
          className="accent-primary cursor-pointer"
        />
        <span
          className={`uppercase font-medium px-1.5 py-0.5 rounded-[--radius-sm] ${TYPE_PILL_CLASSES[item.type]}`}
        >
          {TYPE_LABELS[item.type]}
        </span>
        <span>{item.channel}</span>
        <span className="font-mono">
          {formatRelative(item.timestampIso)}
        </span>
        <span className="text-muted-foreground font-mono text-[10px]">
          {formatAbsoluteWithSeconds(item.timestampIso)}
        </span>
        {item.isError && <span className="text-danger font-medium">error</span>}
        {item.trimmed && (
          <span
            className="uppercase font-medium px-1.5 py-0.5 rounded-[--radius-sm] bg-warning-border/20 text-warning-border"
            title="Dropped from LLM context by the agent's trim path. Still visible here for audit."
          >
            trimmed
          </span>
        )}
      </div>
      <ActivityRowDetail item={item} />
    </li>
  );
}

function ActivityRowDetail({ item }: { item: ActivityItem }) {
  const { expandedTools, expandedReasoning, toggleTool, toggleReasoning } =
    useExpansion();
  if (item.raw.kind === 'user-message') {
    const m = item.raw.message;
    return (
      <div className="text-sm whitespace-pre-wrap break-words">
        {m.body || <em className="text-muted-foreground">(empty)</em>}
      </div>
    );
  }
  if (item.raw.kind === 'agent-reply') {
    const m = item.raw.message;
    const turn = item.raw.turn;
    const errorCount = turn.tool_calls.filter(c => c.is_error).length;
    return (
      <div className="space-y-2">
        <div className="text-sm whitespace-pre-wrap break-words">
          {m.body || <em className="text-muted-foreground">(empty)</em>}
        </div>
        {m.thinking && (
          <ReasoningRow
            text={m.thinking}
            expanded={expandedReasoning.has(item.key)}
            onToggle={() => toggleReasoning(item.key)}
          />
        )}
        {turn.tool_calls.length > 0 && (
          <div className="text-xs">
            <div className="text-muted-foreground mb-1">
              {turn.tool_calls.length} tool call{turn.tool_calls.length === 1 ? '' : 's'}
              {errorCount > 0 && (
                <span className="text-danger font-medium">
                  {' '}· {errorCount} error{errorCount === 1 ? '' : 's'}
                </span>
              )}
            </div>
            <ul className="border-l-2 border-border pl-3 space-y-1">
              {turn.tool_calls.map((c, i) => {
                const key = toolCallKey(item.key, i);
                return (
                  <ToolCallRow
                    key={c.tool_call_id || `${turn.turn_index}-${c.name}`}
                    call={c}
                    expanded={expandedTools.has(key)}
                    onToggle={() => toggleTool(key)}
                  />
                );
              })}
            </ul>
          </div>
        )}
      </div>
    );
  }
  if (item.raw.kind === 'heartbeat') {
    const h = item.raw.data;
    return (
      <div className="text-xs space-y-1">
        <ActivityField label="Action" value={h.action_type} />
        {h.message_text && <ActivityField label="Message" value={h.message_text} />}
        {h.reasoning && <ActivityField label="Reasoning" value={h.reasoning} />}
        {h.tasks && h.tasks !== '[]' && (
          <ActivityField label="Tasks" value={h.tasks} mono />
        )}
      </div>
    );
  }
  if (item.raw.kind === 'approval') {
    const { request, resolution } = item.raw;
    // Anchor is the request when present (it carries the full description
    // and chat_id; the resolution row blanks those in the OSS audit log).
    // Falls back to the resolution for orphan rows whose request fell
    // outside the date window.
    const anchor = request ?? resolution!;
    const resolutionLabel = !resolution
      ? null
      : resolution.event_type === 'decided'
        ? (resolution.decision ?? 'decided')
        : resolution.event_type === 'timed_out'
          ? 'timed out'
          : resolution.event_type === 'recovered'
            ? 'recovered after restart'
            : resolution.event_type;
    return (
      <div className="text-xs space-y-1">
        <ActivityField label="Tool" value={anchor.tool_name} mono />
        {anchor.description && (
          <ActivityField label="Description" value={anchor.description} />
        )}
        {request && (
          <ActivityField
            label="Requested"
            value={formatAbsoluteWithSeconds(request.created_at!)}
            mono
          />
        )}
        {resolution && resolutionLabel && (
          <ActivityField
            label={resolution.event_type === 'decided' ? 'Decision' : 'Resolution'}
            value={
              request
                ? `${resolutionLabel} · ${formatAbsoluteWithSeconds(resolution.created_at!)}`
                : resolutionLabel
            }
          />
        )}
        {!resolution && <ActivityField label="Status" value="pending" />}
        {anchor.chat_id && <ActivityField label="Chat" value={anchor.chat_id} mono />}
      </div>
    );
  }
  const e = item.raw.data;
  const seqRange = formatSeqRange(e.min_message_seq, e.max_message_seq);
  return (
    <div className="text-xs space-y-1">
      <div className="text-[10px] text-muted-foreground flex flex-wrap items-center gap-2">
        <CompactionStatusPill status={e.status} />
        {seqRange && <span className="font-mono">{seqRange}</span>}
      </div>
      <ActivityField
        label="Trimmed"
        value={`${e.trimmed_count} msg / ${e.trimmed_chars.toLocaleString()} chars`}
      />
      <ActivityField
        label="Tokens"
        value={`${e.input_tokens.toLocaleString()} in / ${e.output_tokens.toLocaleString()} out`}
      />
      <ActivityField label="Duration" value={`${e.duration_ms}ms`} />
      <ActivityField label="Summary length" value={String(e.summary_len)} />
      <ActivityField
        label="Updated"
        value={
          [
            e.memory_updated && 'memory',
            e.user_profile_updated && 'user',
            e.soul_updated && 'soul',
          ]
            .filter(Boolean)
            .join(', ') || '(nothing)'
        }
      />
      <div className="mt-2 space-y-3">
        <div>
          <div className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
            Memory file diffs
          </div>
          <CompactionSnapshotDiffs event={e} />
        </div>
        <div>
          <div className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
            LLM call
          </div>
          <CompactionLLMCall event={e} />
        </div>
      </div>
    </div>
  );
}

function ActivityField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="text-xs">
      <span className="text-muted-foreground">{label}:</span>{' '}
      {value.length > 80 || value.includes('\n') ? (
        <pre
          className={`whitespace-pre-wrap break-words mt-0.5 ${mono ? 'font-mono' : ''} bg-card rounded-[--radius-sm] p-2`}
        >
          {value}
        </pre>
      ) : (
        <span className={mono ? 'font-mono' : ''}>{value}</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Share snippet: an admin highlights one or more activity rows and pulls
// them into a clean, conversation-styled panel they can screenshot or copy
// as plain text to paste into Slack / email when showing colleagues what
// Clawbolt did well (or poorly). Frontend-only: no public link, no backend
// (issue #607). The panel renders the same rows the feed shows, stripped of
// the admin chrome (filters, pills, tool-call disclosures) so it reads like
// a transcript.
// ---------------------------------------------------------------------------

// Speaker label for a row in the shared snippet. User and agent rows carry
// the conversation; the system event types keep an explicit label so a
// mixed selection still reads unambiguously.
function snippetSpeaker(item: ActivityItem): string {
  switch (item.raw.kind) {
    case 'user-message':
      return 'User';
    case 'agent-reply':
      return 'Clawbolt';
    case 'heartbeat':
      return 'Clawbolt (heartbeat)';
    case 'compaction':
      return 'System (memory compaction)';
    case 'approval':
      return 'System (approval)';
  }
}

// Plain-text body for a row in the shared snippet. Conversation rows use the
// message body; an agent reply that is pure tool calls (empty body) is
// labeled so the transcript does not show a blank turn (the tool list below
// carries the detail). System rows reuse the feed summary, which is already
// human-readable.
function snippetBody(item: ActivityItem): string {
  switch (item.raw.kind) {
    case 'user-message':
      return item.raw.message.body || '(empty)';
    case 'agent-reply':
      return item.raw.message.body || '(no reply text)';
    case 'heartbeat':
    case 'compaction':
    case 'approval':
      return item.summary;
  }
}

// Tool calls fired during an agent reply; empty for every other row type.
function snippetToolCalls(item: ActivityItem): SharedDataToolCall[] {
  return item.raw.kind === 'agent-reply' ? item.raw.turn.tool_calls : [];
}

// Extended-thinking text for an agent reply; '' for every other row type.
function snippetReasoning(item: ActivityItem): string {
  return item.raw.kind === 'agent-reply' ? item.raw.message.thinking : '';
}

// Collapse whitespace and cap an expanded payload so a noisy result (or a
// stack trace) does not blow up the shared snippet. The ellipsis keeps it
// obvious the value was clipped.
const MAX_SNIPPET_FIELD = 400;
function clip(s: string, max = MAX_SNIPPET_FIELD): string {
  const clean = s.replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

// One ``label: value`` line per populated field of an expanded tool call,
// mirroring the args / result / receipt panes the feed shows when that call
// is opened.
function snippetToolFieldLines(call: SharedDataToolCall): string[] {
  const out: string[] = [];
  if (Object.keys(call.args).length > 0) {
    out.push(`      args: ${clip(JSON.stringify(call.args))}`);
  }
  if (call.result.trim()) {
    out.push(`      result: ${clip(call.result)}`);
  }
  if (call.receipt) {
    out.push(`      receipt: ${clip(JSON.stringify(call.receipt))}`);
  }
  return out;
}

// Plain-text lines for an agent reply's tool calls: a count header (with
// failures called out) and one ✓/✗ line per call. A call's args / result /
// receipt are included only when the admin has that call expanded in the
// feed, so the snippet mirrors exactly what is on screen (issue #607).
// Returns [] when there are no tool calls so the caller can omit the section.
function snippetToolTextLines(
  item: ActivityItem,
  expandedTools: Set<string>,
): string[] {
  const calls = snippetToolCalls(item);
  if (calls.length === 0) return [];
  const failed = calls.filter(c => c.is_error).length;
  const lines = [`  Tools (${calls.length}${failed ? `, ${failed} failed` : ''}):`];
  calls.forEach((c, i) => {
    lines.push(`    ${c.is_error ? '✗' : '✓'} ${c.name}`);
    if (expandedTools.has(toolCallKey(item.key, i))) {
      lines.push(...snippetToolFieldLines(c));
    }
  });
  return lines;
}

// Reasoning line, included only when the admin has the reply's reasoning
// expanded in the feed.
function snippetReasoningTextLines(
  item: ActivityItem,
  expandedReasoning: Set<string>,
): string[] {
  const text = snippetReasoning(item);
  if (!text || !expandedReasoning.has(item.key)) return [];
  return [`  Reasoning: ${clip(text)}`];
}

// Render the highlighted rows as a copyable transcript that mirrors the
// feed's current expand state. Oldest-first so it reads top-to-bottom like a
// conversation, regardless of the feed's newest-first ordering. Wrapped in a
// ``` code fence so the indentation and ✓/✗ marks survive a paste into Slack
// or GitHub (issue #607).
function buildSnippetText(
  items: ActivityItem[],
  expandedTools: Set<string>,
  expandedReasoning: Set<string>,
): string {
  const body = items
    .map(item =>
      [
        `${snippetSpeaker(item)}: ${snippetBody(item)}`,
        ...snippetReasoningTextLines(item, expandedReasoning),
        ...snippetToolTextLines(item, expandedTools),
      ].join('\n'),
    )
    .join('\n\n');
  return `\`\`\`\n${body}\n\`\`\``;
}

// One labeled mono block (args / result / receipt) for an expanded tool call
// in the snippet panel, mirroring the feed's ToolCallRow detail.
function SnippetField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] text-muted-foreground uppercase">{label}</div>
      <pre className="mt-0.5 whitespace-pre-wrap break-words bg-card rounded-[--radius-sm] p-2 text-[11px] font-mono">
        {value}
      </pre>
    </div>
  );
}

function SnippetMessage({
  item,
  expandedTools,
  expandedReasoning,
}: {
  item: ActivityItem;
  expandedTools: Set<string>;
  expandedReasoning: Set<string>;
}) {
  const isUser = item.raw.kind === 'user-message';
  const isAgent = item.raw.kind === 'agent-reply';
  const tone = isUser
    ? 'bg-primary/10 border-primary/20'
    : isAgent
      ? 'bg-panel border-border'
      : 'bg-muted border-border';
  const calls = snippetToolCalls(item);
  const failed = calls.filter(c => c.is_error).length;
  const reasoning = snippetReasoning(item);
  const showReasoning = reasoning !== '' && expandedReasoning.has(item.key);
  return (
    <div className={`rounded-[--radius-md] border p-3 ${tone}`}>
      <div className="flex items-center gap-2 text-[10px] uppercase font-medium text-muted-foreground mb-1">
        <span>{snippetSpeaker(item)}</span>
        <span className="font-mono normal-case">
          {formatAbsoluteWithSeconds(item.timestampIso)}
        </span>
      </div>
      <div className="text-sm whitespace-pre-wrap break-words">
        {snippetBody(item)}
      </div>
      {showReasoning && (
        <div className="mt-2">
          <div className="text-[10px] text-muted-foreground uppercase mb-1">
            Reasoning
          </div>
          <pre className="whitespace-pre-wrap break-words bg-card rounded-[--radius-sm] p-2 text-[11px] font-mono">
            {reasoning}
          </pre>
        </div>
      )}
      {calls.length > 0 && (
        <div className="mt-2 text-xs">
          <div className="text-muted-foreground mb-1">
            {calls.length} tool call{calls.length === 1 ? '' : 's'}
            {failed > 0 && (
              <span className="text-danger font-medium">
                {' '}· {failed} failed
              </span>
            )}
          </div>
          <ul className="border-l-2 border-border pl-3 space-y-1">
            {calls.map((c, i) => {
              const open = expandedTools.has(toolCallKey(item.key, i));
              return (
                <li
                  key={c.tool_call_id || `${i}-${c.name}`}
                  className="text-[11px]"
                >
                  <span
                    className={`font-mono break-words ${c.is_error ? 'text-danger' : 'text-success'}`}
                  >
                    {c.is_error ? '✗' : '✓'} {c.name}
                  </span>
                  {open && (
                    <div className="mt-1 ml-3 space-y-1">
                      {Object.keys(c.args).length > 0 && (
                        <SnippetField
                          label="args"
                          value={JSON.stringify(c.args, null, 2)}
                        />
                      )}
                      {c.result.trim() && (
                        <SnippetField label="result" value={c.result} />
                      )}
                      {c.receipt && (
                        <SnippetField
                          label="receipt"
                          value={JSON.stringify(c.receipt, null, 2)}
                        />
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function ShareSnippetDialog({
  items,
  expandedTools,
  expandedReasoning,
  onClose,
}: {
  items: ActivityItem[];
  expandedTools: Set<string>;
  expandedReasoning: Set<string>;
  onClose: () => void;
}) {
  const copyRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  // Ref on the inner transcript node (not the scroll wrapper) so the image
  // export captures every turn at full height, even when the panel scrolls
  // (issue #607). A long snippet is exactly where screenshotting falls
  // short, so the PNG covers the whole thing.
  const panelRef = useRef<HTMLDivElement>(null);
  const [downloading, setDownloading] = useState(false);
  // Oldest-first for a natural transcript reading order; the feed itself
  // is newest-first, so sort a copy here rather than mutating ``items``.
  const ordered = useMemo(
    () => [...items].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime()),
    [items],
  );

  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    copyRef.current?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const prev = previouslyFocusedRef.current;
      if (prev && document.contains(prev)) prev.focus();
      previouslyFocusedRef.current = null;
    };
  }, [onClose]);

  const handleCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(
        buildSnippetText(ordered, expandedTools, expandedReasoning),
      );
      toast.success('Snippet copied to clipboard');
    } catch {
      toast.error('Could not copy: select the text and copy manually');
    }
  };

  // Rasterize the full transcript node to a PNG and save it. Download (not
  // clipboard) so it works the same in every browser; the admin drags the
  // file into Slack. ``pixelRatio: 2`` keeps it crisp on retina displays.
  //
  // Render to a Blob object URL (not a ``data:`` URL) and click an anchor that
  // is attached to the document. Firefox for Android ignores a programmatic
  // click on a detached <a> and is unreliable with oversized ``data:`` URLs,
  // so the desktop shortcut (toPng -> detached link) silently no-ops on mobile.
  // Revoke on the next tick: revoking synchronously after click() can race the
  // browser's download handoff and cancel the save.
  const handleDownloadImage = async (): Promise<void> => {
    const node = panelRef.current;
    if (!node || downloading) return;
    setDownloading(true);
    try {
      const blob = await toBlob(node, { pixelRatio: 2 });
      if (!blob) throw new Error('Failed to render snippet image');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.download = 'clawbolt-snippet.png';
      link.href = url;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast.success('Snippet image downloaded');
    } catch {
      toast.error('Could not generate image: try Copy as text instead');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="share-snippet-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      <div
        className="absolute inset-0 bg-foreground/30 backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="relative bg-card border border-border rounded-[--radius-lg] shadow-lg max-w-2xl w-full p-5 animate-[dialog-in_150ms_ease-out] flex flex-col max-h-[85vh]">
        <h3 id="share-snippet-title" className="text-base font-semibold mb-1">
          Share conversation snippet
        </h3>
        <p className="text-xs text-muted-foreground mb-3">
          Mirrors what you have expanded in the feed (tool results, reasoning).
          Copy it as text for a Slack thread, or download a PNG of the whole
          panel (handy when the transcript is too long to screenshot).
        </p>
        <div className="flex-1 overflow-y-auto bg-background border border-border rounded-[--radius-md]">
          <div ref={panelRef} className="space-y-2 p-3 bg-background">
            {ordered.map(item => (
              <SnippetMessage
                key={item.key}
                item={item}
                expandedTools={expandedTools}
                expandedReasoning={expandedReasoning}
              />
            ))}
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-2 text-sm font-medium rounded-[--radius-md] border border-border hover:bg-secondary-hover"
          >
            Close
          </button>
          <button
            type="button"
            onClick={() => void handleDownloadImage()}
            disabled={downloading}
            className="px-3 py-2 text-sm font-medium rounded-[--radius-md] border border-border hover:bg-secondary-hover disabled:opacity-50"
          >
            {downloading ? 'Rendering...' : 'Download image'}
          </button>
          <button
            ref={copyRef}
            type="button"
            onClick={() => void handleCopy()}
            className="px-3 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover"
          >
            Copy as text
          </button>
        </div>
      </div>
    </div>
  );
}
