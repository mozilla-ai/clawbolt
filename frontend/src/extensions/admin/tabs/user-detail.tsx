import { useState, useEffect, useCallback, useMemo, type KeyboardEvent, type ReactNode } from 'react';
import { toast } from '@/lib/toast';
import {
  compactUserContext,
  exportUserLLMPayloads,
  getLLMUsageLogs,
  getUserDetail,
  getUserLLMOverride,
  getUserUsage,
  updateUserLLMOverride,
  updateUserPlan,
  activateUser,
  deactivateUser,
  deleteUser,
  resetUserQuota,
  type AdminChannelRouteEntry,
  type AdminToolConfigEntry,
  type AdminUser,
  type AdminUserDetail,
  type AdminUserLLMOverride,
  type AdminUserPermissions,
  type AdminUserUsage,
  type LLMUsageLogItem,
  type SharedDataUser,
} from '../admin-api';
import {
  SharedActivityView,
  SharedMemoryView,
  SharedProfileView,
} from './shared';
import { LLMModelSelect, LLMProviderSelect } from '../llm-picker';
import ConfirmDialog from '../ConfirmDialog';
import ConsentBadge from '../components/ConsentBadge';
import {
  formatAbsolute,
  formatRelative,
  planPillClass,
} from '../format';

/**
 * Convert an ``AdminUser`` row into the ``SharedDataUser`` shape the
 * shared-content components expect. The fields line up except for the
 * consent timestamp's name (``data_sharing_consent_at`` vs ``consent_at``)
 * and the optional ``conversation_count``. Doing this here keeps the
 * shared components free of admin-router-specific shapes.
 */
function toSharedUserShape(user: AdminUser): SharedDataUser {
  return {
    id: user.id,
    user_id: user.user_id,
    email: user.email,
    consent_at: user.data_sharing_consent_at ?? null,
    conversation_count: user.conversation_count ?? 0,
    last_message_at: user.last_message_at ?? null,
  };
}

// ---------------------------------------------------------------------------
// Small helpers shared across sub-views
// ---------------------------------------------------------------------------

function TimeText({ ts }: { ts: string | null | undefined }) {
  if (!ts) return <span className="text-muted-foreground">—</span>;
  return (
    <span title={formatAbsolute(ts)} className="text-muted-foreground">
      {formatRelative(ts)}
    </span>
  );
}

function Pill({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'success' | 'danger' | 'primary' | 'muted' }) {
  const map = {
    neutral: 'bg-panel text-foreground',
    success: 'bg-success-bg text-success',
    danger: 'bg-danger-light text-danger',
    primary: 'bg-primary-light text-primary',
    muted: 'bg-panel text-muted-foreground',
  } as const;
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-[--radius-full] font-medium ${map[tone]}`}>
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Plan toggle (inline segmented control next to the plan pill)
// ---------------------------------------------------------------------------

const PLAN_OPTIONS: readonly string[] = ['free', 'pro'];

interface PlanToggleProps {
  userId: string;
  currentPlan: string;
  activeTone: string;
  disabled: boolean;
  onChanged: () => void;
  onLoadingChange: (loading: boolean) => void;
}

function PlanToggle({
  userId,
  currentPlan,
  activeTone,
  disabled,
  onChanged,
  onLoadingChange,
}: PlanToggleProps) {
  const setPlan = async (plan: string) => {
    if (plan === currentPlan) return;
    onLoadingChange(true);
    try {
      await updateUserPlan(userId, plan);
      toast.success(`Plan set to ${plan}`);
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      onLoadingChange(false);
    }
  };

  return (
    <span
      role="group"
      aria-label="User plan"
      className="inline-flex items-center rounded-[--radius-full] border border-border overflow-hidden text-[11px] font-medium"
    >
      {PLAN_OPTIONS.map((plan) => {
        const isActive = plan === currentPlan;
        return (
          <button
            key={plan}
            type="button"
            disabled={disabled || isActive}
            onClick={() => void setPlan(plan)}
            aria-pressed={isActive}
            title={isActive ? `Current plan: ${plan}` : `Switch to ${plan}`}
            className={
              isActive
                ? `px-1.5 py-0.5 ${activeTone}`
                : 'px-1.5 py-0.5 text-muted-foreground hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed'
            }
          >
            {plan}
          </button>
        );
      })}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Identity header
// ---------------------------------------------------------------------------

interface IdentityHeaderProps {
  detail: AdminUserDetail;
  user: AdminUser;
  onChanged: () => void;
  onDeleted: () => void;
  isSelf: boolean;
}

function IdentityHeader({ detail, user, onChanged, onDeleted, isSelf }: IdentityHeaderProps) {
  const [actionLoading, setActionLoading] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [compactOpen, setCompactOpen] = useState(false);
  const [compactKeepRecent, setCompactKeepRecent] = useState<string>('0');
  const [compactHint, setCompactHint] = useState<string>('');

  const runAction = async (
    successMessage: string,
    action: () => Promise<void>,
  ) => {
    setActionLoading(true);
    try {
      await action();
      toast.success(successMessage);
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const doDelete = async () => {
    setActionLoading(true);
    try {
      await deleteUser(detail.id);
      toast.success('User deleted');
      setDeleteOpen(false);
      onDeleted();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const openCompact = () => {
    // Reset form state every time the dialog opens so a previously
    // typed hint cannot leak across users or actions.
    setCompactKeepRecent('0');
    setCompactHint('');
    setCompactOpen(true);
  };

  const doCompact = async () => {
    const parsed = Number.parseInt(compactKeepRecent, 10);
    const keepRecent = Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    const hint = compactHint;
    // Close the modal up front and run the compaction in the background.
    // The backend call invokes the compaction LLM and routinely takes 10+
    // seconds; leaving the dialog open in a "Working..." state feels frozen.
    // The trigger button is gated on `actionLoading`, so the user cannot
    // double-fire while the request is in flight.
    setCompactOpen(false);
    toast.success('Compaction started; this can take a moment...');
    setActionLoading(true);
    try {
      const result = await compactUserContext(detail.id, { keepRecent, hint });
      if (result.compacted_message_count === 0) {
        toast.success('Nothing to compact (no visible messages above the watermark)');
      } else {
        toast.success(
          `Compacted ${result.compacted_message_count} message(s); ` +
            (result.memory_updated ? 'memory updated' : 'memory unchanged'),
        );
      }
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const doExportLLMPayloads = async () => {
    setActionLoading(true);
    try {
      const data = await exportUserLLMPayloads(detail.id);
      // The backend already sets Content-Disposition, but openapi-fetch
      // unwraps to JSON; we synthesize the download client-side from
      // the parsed body. Pretty-print so the file is grep-friendly when
      // an admin opens it in an editor.
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `llm-payloads-${detail.id}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast.success('LLM payloads exported');
    } catch (e) {
      // ``throwApiError`` (admin-api.ts) surfaces the backend ``detail``
      // string when present, so a 404 here shows the specific message
      // ("No captured payloads for this user") rather than the generic
      // "Failed to export LLM payloads" fallback. This keeps the
      // freshly-consenting-but-no-messages-yet case readable.
      toast.error((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const planTone = planPillClass(detail.plan);

  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-4 space-y-3">
      <div className="flex flex-col md:flex-row md:items-start gap-3 md:gap-4">
        <div className="flex-1 min-w-0">
          <h2 className="text-base font-semibold truncate">
            {detail.email || detail.user_id}
            {isSelf && (
              <span className="ml-2 text-xs text-muted-foreground font-normal">(you)</span>
            )}
          </h2>
          <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
            <PlanToggle
              userId={detail.id}
              currentPlan={detail.plan}
              activeTone={planTone}
              disabled={actionLoading}
              onChanged={() => {
                onChanged();
              }}
              onLoadingChange={setActionLoading}
            />
            {detail.role === 'admin' && <Pill tone="primary">admin</Pill>}
            {detail.is_active ? (
              <Pill tone="success">active</Pill>
            ) : (
              <Pill tone="danger">inactive</Pill>
            )}
            {detail.status && detail.status !== 'active' && detail.status !== 'none' && (
              <Pill tone="muted">subscription: {detail.status}</Pill>
            )}
            {detail.onboarding_complete ? null : (
              <Pill tone="muted">onboarding pending</Pill>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={actionLoading || !user.data_sharing_consent}
            title={
              user.data_sharing_consent
                ? 'Download the captured LLM request payloads (current + previous era) for offline analysis'
                : 'Disabled: user has not opted into data sharing'
            }
            onClick={() => void doExportLLMPayloads()}
          >
            Export LLM payloads
          </button>
          <button
            type="button"
            className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={actionLoading || isSelf}
            title={isSelf ? 'You cannot reset your own quota' : undefined}
            onClick={() => void runAction('Quota reset', () => resetUserQuota(detail.id))}
          >
            Reset quota
          </button>
          <button
            type="button"
            className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={actionLoading}
            title="Run an admin compaction over the user's currently-visible conversation context. Durable facts are extracted into MEMORY/USER/SOUL before the trim watermark advances."
            onClick={openCompact}
          >
            Compact context...
          </button>
          {detail.is_active ? (
            <button
              type="button"
              className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={actionLoading || isSelf}
              title={isSelf ? 'You cannot deactivate your own account' : undefined}
              onClick={() => void runAction('User deactivated', () => deactivateUser(detail.id))}
            >
              Deactivate
            </button>
          ) : (
            <button
              type="button"
              className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={actionLoading || isSelf}
              title={isSelf ? 'You cannot reactivate your own account' : undefined}
              onClick={() => void runAction('User activated', () => activateUser(detail.id))}
            >
              Activate
            </button>
          )}
          <button
            type="button"
            className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={actionLoading || isSelf}
            title={isSelf ? 'You cannot delete your own account' : undefined}
            onClick={() => setDeleteOpen(true)}
          >
            Delete user...
          </button>
        </div>
      </div>

      <dl className="grid grid-cols-2 sm:grid-cols-3 gap-y-1.5 gap-x-4 text-xs pt-2 border-t border-border/50">
        <div>
          <dt className="text-muted-foreground">Signed up</dt>
          <dd><TimeText ts={detail.subscription_created_at} /></dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Last login</dt>
          <dd>
            <TimeText ts={user.last_login_at} />
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Messages (month)</dt>
          <dd className="text-foreground">{user.messages_this_month ?? 0}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Timezone</dt>
          <dd className="text-foreground truncate">{detail.timezone || <span className="text-muted-foreground italic">—</span>}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Preferred channel</dt>
          <dd className="text-foreground">{detail.preferred_channel || <span className="text-muted-foreground italic">—</span>}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Heartbeats</dt>
          <dd>
            {detail.heartbeat_opt_in ? (
              <span className="inline-flex items-center gap-1">
                <Pill tone="success">on</Pill>
                <span className="text-foreground">{detail.heartbeat_frequency || '—'}</span>
              </span>
            ) : (
              <Pill tone="muted">off</Pill>
            )}
          </dd>
        </div>
      </dl>

      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={doDelete}
        title={`Delete ${detail.email || detail.user_id}?`}
        description={
          <p>
            This permanently removes the user, all chat sessions, memory documents,
            media files, heartbeat logs, and quotas. It cannot be undone.
          </p>
        }
        confirmLabel="Delete user"
        destructive
        busy={actionLoading}
      />

      <ConfirmDialog
        open={compactOpen}
        onClose={() => setCompactOpen(false)}
        onConfirm={doCompact}
        title={`Compact context for ${detail.email || detail.user_id}?`}
        description={
          <div className="space-y-3">
            <p>
              Runs an admin compaction over every conversation message currently
              visible to the agent. Durable facts (clients, jobs, pricing, profile
              info) are extracted into MEMORY.md / USER.md / SOUL.md, then the
              trim watermark advances so the next turn starts fresh.
            </p>
            <p className="text-warning">
              This rewrites the user&apos;s persistent memory files and cannot be
              undone. Use it to clear poisoned context (e.g. after the agent
              learned a wrong fact about its own capabilities), not for routine
              hygiene.
            </p>
            <div className="space-y-1">
              <label
                htmlFor="compact-keep-recent"
                className="block text-xs font-medium text-foreground"
              >
                Keep recent (turns to preserve)
              </label>
              <input
                id="compact-keep-recent"
                type="number"
                min={0}
                value={compactKeepRecent}
                onChange={(e) => setCompactKeepRecent(e.target.value)}
                disabled={actionLoading}
                className="w-24 px-2 py-1 text-sm rounded-[--radius-sm] border border-border bg-background"
              />
              <p className="text-xs text-muted-foreground">
                Preserves the last N visible messages. Use a small value (e.g. 2)
                if the user has a pending request you do not want to lose.
              </p>
            </div>
            <div className="space-y-1">
              <label
                htmlFor="compact-hint"
                className="block text-xs font-medium text-foreground"
              >
                Steering hint (optional)
              </label>
              <textarea
                id="compact-hint"
                rows={3}
                value={compactHint}
                onChange={(e) => setCompactHint(e.target.value)}
                disabled={actionLoading}
                placeholder='e.g. "ignore prior agent self-claims about AppFolio capabilities"'
                className="w-full px-2 py-1 text-sm rounded-[--radius-sm] border border-border bg-background"
              />
              <p className="text-xs text-muted-foreground">
                Prepended to the compaction LLM&apos;s conversation block so it can
                be biased about how to read poisoned messages.
              </p>
            </div>
          </div>
        }
        confirmLabel="Compact context"
        destructive
        busy={actionLoading}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-tabs
// ---------------------------------------------------------------------------
//
// User-detail is now the single per-user monitoring surface. For
// consenting users (``data_sharing_consent=true``) Activity / Memory
// render the consent-gated content imported from shared.tsx. For
// non-consenting users the same tabs render an "explain consent"
// empty state, so the structural mental model is the same across both
// populations.
//
// The standalone Conversation sub-tab was folded into Activity in #404:
// activity rows now show full message bodies and tool calls inline,
// which subsumes the conversation transcript while keeping the
// activity tab's filter / search / timeline affordances.
//
// Reported is still a top-level admin tab in ../index.tsx because the
// underlying queue is global. Cross-links from Reported back into
// User-detail land in PR6.

const SUB_TABS = [
  { id: 'activity', label: 'Activity', disabled: false },
  { id: 'memory', label: 'Memory', disabled: false },
  { id: 'llm', label: 'LLM', disabled: false },
  { id: 'profile', label: 'Profile', disabled: false },
] as const;

// Narrowed to entries with ``disabled: false``. The active section and the
// ``SubTabBar.onChange`` callback both use this; disabled tabs can't become
// active state.
type EnabledSubTabId = Extract<(typeof SUB_TABS)[number], { disabled: false }>['id'];

/**
 * The sub-view, as it appears in the URL (``/app/admin/users/{id}/{section}``).
 * Exported so the router can validate the path segment before handing it back.
 */
export type UserDetailSection = EnabledSubTabId;

export function isUserDetailSection(value: string | undefined): value is UserDetailSection {
  return SUB_TABS.some(t => !t.disabled && t.id === value);
}

// The previous standalone "Heartbeats" sub-tab and its action-type pill
// helper were folded into the Activity timeline imported from shared.tsx.
// Activity merges heartbeats with conversations and compactions for
// consenting users, and falls back to a metadata-only timeline for
// non-consenting ones, so a separate Heartbeats tab is redundant.

// ---------------------------------------------------------------------------
// Consent gate empty state
// ---------------------------------------------------------------------------

function ConsentRequiredPanel({ what }: { what: string }) {
  return (
    <div className="bg-card border border-border rounded-[--radius-md] p-4 text-sm">
      <div className="flex items-start gap-2">
        <svg className="w-5 h-5 text-muted-foreground mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 15v2m0-10a4 4 0 00-4 4v3h8v-3a4 4 0 00-4-4z" />
          <rect x="4" y="11" width="16" height="10" rx="2" />
        </svg>
        <div>
          <p className="font-medium text-foreground">{what} requires data sharing consent</p>
          <p className="mt-1 text-xs text-muted-foreground">
            This user has not opted into research data sharing, so message
            bodies, memory text, and heartbeat content are not available.
            The user can opt in via the Account page if they choose; the
            opt-in is auditable and revocable at any time.
          </p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LLM sub-tab: per-user override + per-call usage logs
// ---------------------------------------------------------------------------

function UserLLMOverrideSection({ userId }: { userId: string }) {
  const [override, setOverride] = useState<AdminUserLLMOverride | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [providerInput, setProviderInput] = useState('');
  const [modelInput, setModelInput] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getUserLLMOverride(userId)
      .then(o => {
        setOverride(o);
        setProviderInput(o.llm_provider_override);
        setModelInput(o.llm_model_override);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [userId]);

  useEffect(() => {
    load();
  }, [load]);

  // The model picker drives off the explicit override-provider when it is
  // set, otherwise the resolved effective provider so the admin still sees
  // a meaningful list when the override is blank.
  const lookupProvider = providerInput || override?.effective_llm_provider || '';

  // Switching the provider override invalidates any model the previous
  // provider knew about. Blank the model so the picker forces a fresh
  // choice; admins who clear the provider back to "use global" likewise
  // shouldn't keep a stale model override pinned.
  const handleProviderChange = (next: string) => {
    if (next !== providerInput) {
      setProviderInput(next);
      setModelInput('');
    }
  };

  if (loading) return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;
  if (error || !override) {
    return (
      <div className="text-danger text-sm">
        {error || 'No override data'}{' '}
        <button type="button" className="text-primary hover:underline" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  const hasChanges =
    providerInput !== override.llm_provider_override ||
    modelInput !== override.llm_model_override;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!hasChanges) return;
    setSaving(true);
    try {
      const updated = await updateUserLLMOverride(userId, {
        llm_provider_override: providerInput,
        llm_model_override: modelInput,
      });
      setOverride(updated);
      setProviderInput(updated.llm_provider_override);
      setModelInput(updated.llm_model_override);
      toast.success('Per-user LLM override saved');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async () => {
    setSaving(true);
    try {
      const updated = await updateUserLLMOverride(userId, {
        llm_provider_override: '',
        llm_model_override: '',
      });
      setOverride(updated);
      setProviderInput('');
      setModelInput('');
      toast.success('Override cleared, user falls back to global default');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const isOverridden =
    !!override.llm_provider_override || !!override.llm_model_override;

  return (
    <form
      className="bg-card border border-border rounded-[--radius-md] p-4 mb-4"
      onSubmit={handleSubmit}
    >
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <h3 className="text-sm font-semibold">Per-user LLM override</h3>
        {isOverridden ? (
          <Pill tone="primary">override active</Pill>
        ) : (
          <Pill tone="muted">using global default</Pill>
        )}
      </div>
      <p className="text-xs text-muted-foreground mb-3">
        Effective: <span className="font-mono">{override.effective_llm_provider}</span>
        {' / '}
        <span className="font-mono">{override.effective_llm_model}</span>.
        Pick "(use global default)" to fall back for that field.
      </p>
      <p className="text-xs text-muted-foreground mb-3">
        Applies to the main agent loop (chat replies). Heartbeat,
        memory compaction, and vision still use the global default.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label
            htmlFor="user-llm-provider"
            className="text-xs text-muted-foreground block mb-1"
          >
            Provider override
          </label>
          <LLMProviderSelect
            id="user-llm-provider"
            value={providerInput}
            onChange={handleProviderChange}
            allowEmpty
            emptyLabel="(use global default)"
          />
        </div>
        <div>
          <label
            htmlFor="user-llm-model"
            className="text-xs text-muted-foreground block mb-1"
          >
            Model override
          </label>
          <LLMModelSelect
            id="user-llm-model"
            provider={lookupProvider}
            value={modelInput}
            onChange={setModelInput}
            allowEmpty
            emptyLabel="(use global default)"
          />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap justify-end gap-2">
        <button
          type="button"
          className="px-3 py-1.5 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-50"
          disabled={!isOverridden || saving}
          onClick={() => void handleClear()}
        >
          Clear override
        </button>
        <button
          type="submit"
          className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
          disabled={!hasChanges || saving}
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </form>
  );
}

function formatCost(usd: string | number): string {
  const n = typeof usd === 'number' ? usd : Number(usd);
  if (Number.isNaN(n)) return String(usd);
  if (n === 0) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function formatPeriodStart(iso: string | null): string {
  if (!iso) return 'this period';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'this period';
  // The quota period boundary is calendar-month UTC; render in UTC so a
  // west-coast admin viewing a fresh period does not see the prior month
  // (e.g. "Apr 30" for the first 7 hours of UTC May 1).
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

function UsageBar({ used, limit }: { used: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  const pct = Math.round(ratio * 100);
  const tone =
    ratio >= 1
      ? 'bg-danger'
      : ratio >= 0.8
        ? 'bg-warning'
        : 'bg-primary';
  return (
    <div
      className="h-1.5 w-full bg-panel rounded-[--radius-full] overflow-hidden"
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function UsageCard({ userId }: { userId: string }) {
  const [usage, setUsage] = useState<AdminUserUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getUserUsage(userId)
      .then(setUsage)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [userId]);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <div className="animate-pulse h-24 bg-panel rounded-[--radius-md] mb-5" />;
  }

  if (error) {
    return (
      <div className="bg-card border border-border rounded-[--radius-md] p-4 mb-5 text-sm text-danger">
        Failed to load usage: {error}{' '}
        <button type="button" className="text-primary hover:underline" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  if (!usage) return null;

  const msgPct = usage.messages.limit > 0
    ? Math.round((usage.messages.used / usage.messages.limit) * 100)
    : 0;
  const tokPct = usage.tokens.limit > 0
    ? Math.round((usage.tokens.used / usage.tokens.limit) * 100)
    : 0;
  const since = formatPeriodStart(usage.period_start);

  return (
    <section
      aria-labelledby="usage-card-heading"
      className="bg-card border border-border rounded-[--radius-md] p-4 mb-5"
    >
      <div className="flex items-baseline justify-between mb-3">
        <h3 id="usage-card-heading" className="text-sm font-semibold">
          Usage
        </h3>
        <span className="text-xs text-muted-foreground">Since {since}</span>
      </div>
      <dl className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
        <div>
          <div className="flex items-baseline justify-between text-xs mb-1">
            <dt className="text-muted-foreground">Messages</dt>
            <dd className="font-mono">
              {usage.messages.used.toLocaleString()} / {usage.messages.limit.toLocaleString()}
              <span className="text-muted-foreground ml-1">({msgPct}%)</span>
            </dd>
          </div>
          <UsageBar used={usage.messages.used} limit={usage.messages.limit} />
        </div>
        <div>
          <div className="flex items-baseline justify-between text-xs mb-1">
            <dt className="text-muted-foreground">Tokens</dt>
            <dd className="font-mono">
              {formatTokens(usage.tokens.used)} / {formatTokens(usage.tokens.limit)}
              <span className="text-muted-foreground ml-1">({tokPct}%)</span>
            </dd>
          </div>
          <UsageBar used={usage.tokens.used} limit={usage.tokens.limit} />
        </div>
      </dl>
      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs pt-3 border-t border-border">
        <div className="flex items-baseline gap-2">
          <dt className="text-muted-foreground">LLM cost this period</dt>
          <dd className="font-mono">{formatCost(usage.period_cost_usd)}</dd>
        </div>
        <div className="flex items-baseline gap-2">
          <dt className="text-muted-foreground">All-time</dt>
          <dd className="font-mono">{formatCost(usage.lifetime_cost_usd)}</dd>
        </div>
      </dl>
    </section>
  );
}

function UserLLMLogs({ userId }: { userId: string }) {
  const [logs, setLogs] = useState<LLMUsageLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getLLMUsageLogs(userId, 100)
      .then(res => {
        setLogs(res.items);
        setTotal(res.total);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [userId]);

  useEffect(() => { load(); }, [load]);

  const totals = useMemo(() => {
    let inT = 0, outT = 0, costN = 0;
    for (const l of logs) {
      inT += l.input_tokens;
      outT += l.output_tokens;
      const n = Number(l.cost_usd);
      if (!Number.isNaN(n)) costN += n;
    }
    return { input: inT, output: outT, cost: costN };
  }, [logs]);

  if (loading) return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;

  if (error) {
    return (
      <div className="text-danger text-sm">
        {error}{' '}
        <button type="button" className="text-primary hover:underline" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  if (logs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No LLM usage logs found for this user.
      </p>
    );
  }

  return (
    <div>
      <div className="text-xs text-muted-foreground mb-2 flex flex-wrap gap-x-4 gap-y-1">
        <span>{total} total call{total !== 1 ? 's' : ''}</span>
        <span>showing {logs.length}</span>
        <span>{formatTokens(totals.input)} in / {formatTokens(totals.output)} out</span>
        <span>{formatCost(totals.cost)} (visible)</span>
      </div>
      <table className="w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
        <caption className="sr-only">LLM usage log entries</caption>
        <thead className="bg-panel">
          <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-2 py-1.5 font-medium">When</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Purpose</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Provider / Model</th>
            <th scope="col" className="px-2 py-1.5 font-medium text-right">In</th>
            <th scope="col" className="px-2 py-1.5 font-medium text-right">Out</th>
            <th scope="col" className="px-2 py-1.5 font-medium text-right">Cache R/W</th>
            <th scope="col" className="px-2 py-1.5 font-medium text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {logs.map(log => (
            <tr key={log.id} className="border-t border-border">
              <td
                className="px-2 py-1.5 text-muted-foreground whitespace-nowrap"
                title={formatAbsolute(log.timestamp)}
              >
                {formatRelative(log.timestamp)}
              </td>
              <td className="px-2 py-1.5">
                {log.purpose ? <Pill tone="muted">{log.purpose}</Pill> : <span className="text-muted-foreground">—</span>}
              </td>
              <td className="px-2 py-1.5 font-mono text-[11px] break-all">
                {log.provider}<span className="text-muted-foreground">/</span>{log.model}
              </td>
              <td className="px-2 py-1.5 text-right font-mono">{formatTokens(log.input_tokens)}</td>
              <td className="px-2 py-1.5 text-right font-mono">{formatTokens(log.output_tokens)}</td>
              <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                {log.cache_read_input_tokens == null && log.cache_creation_input_tokens == null
                  ? '—'
                  : `${log.cache_read_input_tokens != null ? formatTokens(log.cache_read_input_tokens) : '—'}/${log.cache_creation_input_tokens != null ? formatTokens(log.cache_creation_input_tokens) : '—'}`}
              </td>
              <td className="px-2 py-1.5 text-right font-mono">{formatCost(log.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profile sub-tab: tool configs + channel routes
// ---------------------------------------------------------------------------
//
// The sticky ``ProfileAnchorBar`` jump-nav was removed when Memory / Soul /
// User notes / Heartbeat directives sections were dropped — two sections
// don't need their own table of contents. Bring it back if Profile gains
// 4+ sections again (e.g., when consent-gated content paths land).

function ToolConfigsSection({ items }: { items: AdminToolConfigEntry[] }) {
  // Sub-tool gating moved to user_permissions ("never" level) in OSS #1323.
  // Disabled-here means the whole tool group is off; per-tool "never" entries
  // surface in the Permissions section below.
  const disabled = items.filter(t => !t.enabled);
  const enabled = items.filter(t => t.enabled);

  return (
    <section
      id="profile-tools"
      className="bg-card border border-border rounded-[--radius-md] p-4 target:border-primary target:ring-1 target:ring-primary/40"
    >
      <header className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">Tool configs</h3>
        <span className="text-[10px] text-muted-foreground">{items.length} configured</span>
      </header>
      {items.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground italic">No tool configurations recorded.</p>
      ) : (
        <div className="mt-2 space-y-3">
          {disabled.length > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-danger font-semibold mb-1">
                Disabled
              </p>
              <table className="w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
                <caption className="sr-only">Disabled tools</caption>
                <thead className="bg-panel">
                  <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="px-2 py-1.5 font-medium">Tool</th>
                    <th scope="col" className="px-2 py-1.5 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {disabled.map(t => (
                    <tr key={t.tool_name} className="bg-danger-light/30 border-t border-border min-h-[36px]">
                      <td className="px-2 py-1.5 font-medium">{t.tool_name}</td>
                      <td className="px-2 py-1.5"><Pill tone="danger">disabled</Pill></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {enabled.length > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold mb-1">
                Fully enabled
              </p>
              <table className="w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
                <caption className="sr-only">Fully enabled tools</caption>
                <thead className="bg-panel">
                  <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="px-2 py-1.5 font-medium">Tool</th>
                  </tr>
                </thead>
                <tbody>
                  {enabled.map(t => (
                    <tr key={t.tool_name} className="border-t border-border min-h-[36px]">
                      <td className="px-2 py-1.5 text-muted-foreground">{t.tool_name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function permissionPillTone(level: string): 'success' | 'danger' | 'muted' | 'neutral' {
  switch (level) {
    case 'always':
      return 'success';
    case 'deny':
      return 'danger';
    case 'ask':
      return 'muted';
    default:
      return 'neutral';
  }
}

function PermissionsSection({ permissions }: { permissions: AdminUserPermissions }) {
  // Read-only mirror of what the user sees at GET /user/permissions:
  // approval levels (always / ask / deny) for each tool, plus optional
  // resource-scoped overrides keyed by (tool, resource pattern). We
  // surface this so admins can answer "why did the agent skip / prompt
  // for this tool?" without spelunking into the user_permissions row.
  const { tools, resources } = permissions;
  const total = tools.length + resources.length;
  return (
    <section
      id="profile-permissions"
      className="bg-card border border-border rounded-[--radius-md] p-4 target:border-primary target:ring-1 target:ring-primary/40"
    >
      <header className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">Permissions</h3>
        <span className="text-[10px] text-muted-foreground">
          {tools.length} tool{tools.length === 1 ? '' : 's'}
          {resources.length > 0
            ? `, ${resources.length} resource${resources.length === 1 ? '' : 's'}`
            : ''}
        </span>
      </header>
      {total === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground italic">
          No permission overrides recorded. Each tool falls back to its declared default.
        </p>
      ) : (
        <div className="mt-2 space-y-3">
          {tools.length > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold mb-1">
                Tool levels
              </p>
              <table className="w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
                <caption className="sr-only">Tool-level approval levels</caption>
                <thead className="bg-panel">
                  <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="px-2 py-1.5 font-medium">Tool</th>
                    <th scope="col" className="px-2 py-1.5 font-medium">Level</th>
                  </tr>
                </thead>
                <tbody>
                  {tools.map(t => (
                    <tr key={t.tool_name} className="border-t border-border min-h-[36px]">
                      <td className="px-2 py-1.5 font-medium">{t.tool_name}</td>
                      <td className="px-2 py-1.5">
                        <Pill tone={permissionPillTone(t.level)}>{t.level}</Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {resources.length > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold mb-1">
                Resource overrides
              </p>
              <table className="w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
                <caption className="sr-only">Resource-scoped approval levels</caption>
                <thead className="bg-panel">
                  <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="px-2 py-1.5 font-medium">Tool</th>
                    <th scope="col" className="px-2 py-1.5 font-medium">Resource</th>
                    <th scope="col" className="px-2 py-1.5 font-medium">Level</th>
                  </tr>
                </thead>
                <tbody>
                  {resources.map(r => (
                    <tr
                      key={`${r.tool_name}::${r.resource}`}
                      className="border-t border-border min-h-[36px]"
                    >
                      <td className="px-2 py-1.5 font-medium">{r.tool_name}</td>
                      <td className="px-2 py-1.5 font-mono break-all">{r.resource}</td>
                      <td className="px-2 py-1.5">
                        <Pill tone={permissionPillTone(r.level)}>{r.level}</Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

const STALE_MS = 30 * 24 * 60 * 60 * 1000;

function ChannelRoutesSection({ items }: { items: AdminChannelRouteEntry[] }) {
  return (
    <section
      id="profile-channels"
      className="bg-card border border-border rounded-[--radius-md] p-4 target:border-primary target:ring-1 target:ring-primary/40"
    >
      <header className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">Channels</h3>
        <span className="text-[10px] text-muted-foreground">{items.length} configured</span>
      </header>
      {items.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground italic">No channel routes configured.</p>
      ) : (
        <table className="mt-2 w-full text-xs border border-border rounded-[--radius-sm] overflow-hidden">
          <caption className="sr-only">Channel routes for this user</caption>
          <thead className="bg-panel">
            <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-2 py-1.5 font-medium">Channel</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Identifier</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Enabled</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Last inbound</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c, i) => {
              const isCurrent = i === 0 && !!c.last_inbound_at;
              const stale =
                c.last_inbound_at != null &&
                Date.now() - new Date(c.last_inbound_at).getTime() > STALE_MS;
              return (
                <tr
                  key={`${c.channel}-${c.channel_identifier}`}
                  className={`border-t border-border min-h-[36px] ${
                    isCurrent ? 'border-l-2 border-l-primary' : ''
                  }`}
                >
                  <td className="px-2 py-1.5">
                    <span className="flex items-center gap-1">
                      <span className="font-medium">{c.channel}</span>
                      {isCurrent && <Pill tone="primary">current</Pill>}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 font-mono break-all">{c.channel_identifier}</td>
                  <td className="px-2 py-1.5">
                    {c.enabled ? <Pill tone="success">on</Pill> : <Pill tone="danger">off</Pill>}
                  </td>
                  <td className={`px-2 py-1.5 ${stale ? 'text-muted-foreground' : ''}`}>
                    {c.last_inbound_at ? (
                      <span title={formatAbsolute(c.last_inbound_at)}>
                        {formatRelative(c.last_inbound_at)}
                      </span>
                    ) : (
                      <span className="text-muted-foreground italic">never</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function UserProfile({ detail, user }: { detail: AdminUserDetail; user: AdminUser }) {
  // The Profile tab now layers two surfaces:
  //   1. Always-visible configuration: channel routes, tool configs,
  //      heartbeat opt-in / frequency. These do not require consent
  //      because they're admin-routing metadata, not user content.
  //   2. Consent-gated agent profile text: soul, user notes, heartbeat
  //      directives. These are the actual content the agent reads, so
  //      they're only surfaced when ``data_sharing_consent=true``.
  //
  // Splitting the surface this way is what removed the previous "go
  // look at the Shared tab for content" pointer; everything for one
  // user lives here now.
  return (
    <div className="space-y-4">
      <ChannelRoutesSection items={detail.channel_routes} />
      <ToolConfigsSection items={detail.tool_configs} />
      <PermissionsSection permissions={detail.permissions} />
      <section
        id="profile-text"
        className="bg-card border border-border rounded-[--radius-md] p-4"
      >
        <header className="flex items-center gap-2 mb-2">
          <h3 className="text-sm font-semibold">Agent profile text</h3>
          {user.data_sharing_consent && (
            <ConsentBadge consentAt={user.data_sharing_consent_at} compact />
          )}
        </header>
        {user.data_sharing_consent ? (
          <SharedProfileView user={toSharedUserShape(user)} />
        ) : (
          <p className="text-xs text-muted-foreground">
            Soul, user notes, and heartbeat directives are gated by data
            sharing consent. They surface here once the user opts in.
          </p>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-tab bar
// ---------------------------------------------------------------------------

function SubTabBar({
  active,
  onChange,
}: {
  active: EnabledSubTabId;
  onChange: (id: EnabledSubTabId) => void;
}) {
  // Keyboard navigation skips disabled tabs so arrow-keys land on something
  // interactive instead of bouncing through the placeholder rows. Memoized
  // because SUB_TABS is a const tuple — the filter result is the same on
  // every render, so we shouldn't churn the handler memo.
  const enabledTabs = useMemo(
    () =>
      SUB_TABS.filter(
        (t): t is Extract<(typeof SUB_TABS)[number], { disabled: false }> => !t.disabled,
      ),
    [],
  );
  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      const currentIndex = enabledTabs.findIndex(t => t.id === active);
      if (currentIndex === -1) return;
      let nextIndex = currentIndex;

      if (e.key === 'ArrowRight') {
        nextIndex = (currentIndex + 1) % enabledTabs.length;
      } else if (e.key === 'ArrowLeft') {
        nextIndex = (currentIndex - 1 + enabledTabs.length) % enabledTabs.length;
      } else {
        return;
      }

      e.preventDefault();
      const next = enabledTabs[nextIndex];
      if (next) onChange(next.id);
    },
    [active, onChange, enabledTabs],
  );

  // The right-edge fade (the gradient overlay below) tells users on
  // narrow viewports that there are more tabs offscreen — important
  // because the disabled ``Reported`` / ``Shared`` placeholders sit at
  // the end of the bar and would otherwise be invisible.
  return (
    <div className="relative">
      <div
        className="flex border-b border-border overflow-x-auto gap-1 scrollbar-thin"
        role="tablist"
        onKeyDown={handleKeyDown}
      >
      {SUB_TABS.map(tab => {
        const isActive = active === tab.id;
        const baseClass =
          'px-4 py-2.5 text-[13px] font-medium whitespace-nowrap min-h-[44px] transition-colors flex items-center gap-1.5';
        const stateClass = tab.disabled
          ? 'text-muted-foreground/60 cursor-not-allowed'
          : isActive
            ? 'text-foreground border-b-2 border-primary cursor-pointer'
            : 'text-muted-foreground hover:text-foreground cursor-pointer';
        // ARIA tab pattern: use ``aria-disabled`` (not the HTML ``disabled``
        // attribute) so disabled tabs stay in the accessibility tree and are
        // discoverable to screen readers. The click guard below blocks
        // activation; the visible "Soon" pill is the redundant signal so
        // touch / keyboard-only users don't depend on a hover tooltip.
        return (
          <button
            key={tab.id}
            role="tab"
            type="button"
            aria-selected={isActive}
            aria-disabled={tab.disabled}
            tabIndex={isActive ? 0 : -1}
            title={tab.disabled ? 'Coming soon — surfaces user-reported and shared content' : undefined}
            className={`${baseClass} ${stateClass}`}
            onClick={() => {
              // Inline narrowing so ``tab.id`` types as ``EnabledSubTabId``.
              if (!tab.disabled) onChange(tab.id);
            }}
          >
            {tab.label}
            {tab.disabled && (
              <span className="text-[9px] uppercase tracking-wide bg-panel text-muted-foreground px-1 rounded">
                soon
              </span>
            )}
          </button>
        );
      })}
      </div>
      {/* Scroll-fade overlay on the right edge — purely visual, hidden
          from AT (aria-hidden). Hints that there are more tabs when the
          tablist overflows on narrow viewports. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute right-0 top-0 bottom-0 w-8 bg-gradient-to-l from-background to-transparent"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// User Detail View
// ---------------------------------------------------------------------------

interface UserDetailViewProps {
  user: AdminUser;
  currentUserId?: string;
  /** Which sub-view to render. Comes from the URL so sub-views are linkable. */
  section: UserDetailSection;
  onSectionChange: (section: UserDetailSection) => void;
  onBackToOverview: () => void;
  onBackToUsers: () => void;
}

export default function UserDetailView({
  user,
  currentUserId,
  section,
  onSectionChange,
  onBackToOverview,
  onBackToUsers,
}: UserDetailViewProps) {
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getUserDetail(user.id)
      .then(setDetail)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [user.id]);

  useEffect(() => { load(); }, [load]);

  const isSelf = !!currentUserId && currentUserId === user.id;

  return (
    <div>
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm mb-4" aria-label="Breadcrumb">
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground transition-colors"
          onClick={onBackToOverview}
        >
          Admin
        </button>
        <span className="text-muted-foreground">/</span>
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground transition-colors"
          onClick={onBackToUsers}
        >
          Users
        </button>
        <span className="text-muted-foreground">/</span>
        <span className="text-foreground font-medium truncate max-w-[60vw]">
          {user.email || user.user_id}
        </span>
      </nav>

      {loading ? (
        <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />
      ) : error ? (
        <div className="text-danger text-sm">
          {error}{' '}
          <button type="button" className="text-primary hover:underline" onClick={load}>
            Retry
          </button>
        </div>
      ) : detail ? (
        <>
          <div className="mb-5">
            <IdentityHeader
              detail={detail}
              user={user}
              onChanged={load}
              onDeleted={onBackToUsers}
              isSelf={isSelf}
            />
          </div>

          <UsageCard userId={user.id} />

          <SubTabBar active={section} onChange={onSectionChange} />

          <div className="mt-4" role="tabpanel">
            {section === 'activity' && (
              user.data_sharing_consent ? (
                <SharedActivityView user={toSharedUserShape(user)} />
              ) : (
                <ConsentRequiredPanel what="The full activity timeline" />
              )
            )}
            {section === 'memory' && (
              user.data_sharing_consent ? (
                <SharedMemoryView user={toSharedUserShape(user)} />
              ) : (
                <ConsentRequiredPanel what="Memory and compaction history" />
              )
            )}
            {section === 'llm' && (
              <>
                <UserLLMOverrideSection userId={user.id} />
                <UserLLMLogs userId={user.id} />
              </>
            )}
            {section === 'profile' && <UserProfile detail={detail} user={user} />}
          </div>
        </>
      ) : null}
    </div>
  );
}
