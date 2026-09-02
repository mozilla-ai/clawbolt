import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  cancelEvalRun,
  getAdminUsers,
  getEvalReport,
  listEvalRuns,
  startEvalRun,
  type AdminUser,
  type EvalDecision,
  type EvalRecommendation,
  type EvalReport,
  type EvalRun,
  type EvalSummary,
  type EvalTurn,
} from '../admin-api';
import { LLMProviderSelect, LLMModelSelect } from '../llm-picker';
import { formatRelative } from '../format';
import { adminPath } from '../nav-items';

// Model comparison: "can I move this user to a different model without
// breaking them". A run replays the user's own recent turns through their
// current model and a candidate and reports what each decided.
//
// Three things drive the layout:
//
// - The verdict is stated once, in words, at the top. An operator opening
//   this page has one question, and a grid of rates does not answer it.
// - Safety findings are never mixed into a quality score. They are listed
//   separately and they are what sinks a recommendation, because each one
//   is an action the agent loop would have taken against a real account.
// - The turn drill-down is the point, not an appendix. Numbers persuade
//   nobody about a decision this consequential; reading six diverging turns
//   does. The report arrives worst-first so the turns that matter are the
//   ones on screen.
//
// Only users who opted into data sharing can be evaluated: a run reads their
// real conversations and the report renders them back. The picker is filtered
// to consenting users so the 403 is never reachable from the UI.

// Poll cadence while a run is in flight. A hundred-turn run takes minutes and
// writes a row per turn, so this is fast enough to look alive and slow enough
// not to hammer the report endpoint.
const POLL_MS = 2000;

const SAMPLE_CHOICES = [25, 50, 100, 200];

// Turns are returned worst-first, so the first page is the part that decides
// anything. The rest is available on request rather than shipped by default:
// every text column on a turn is decrypted and PII-scrubbed per request.
const TURN_PAGE_SIZE = 50;

const ACTIVE_STATUSES = new Set(['pending', 'running']);

const RECOMMENDATION_COPY: Record<EvalRecommendation, { label: string; className: string }> = {
  safe_to_switch: {
    label: 'Safe to switch',
    className: 'bg-success-bg text-success-text border-success/30',
  },
  switch_with_monitoring: {
    label: 'Switch with monitoring',
    className: 'bg-warning-bg text-warning-text border-warning/30',
  },
  do_not_switch: {
    label: 'Do not switch',
    className: 'bg-error-bg text-error-text border-danger/30',
  },
  inconclusive: {
    label: 'Inconclusive',
    className: 'bg-panel text-muted-foreground border-border',
  },
};

const AGREEMENT_COPY: Record<string, string> = {
  identical: 'Same action',
  same_tools_different_args: 'Same tools, different arguments',
  different_tools: 'Different tools',
  replied_instead_of_acting: 'Replied instead of acting',
  acted_instead_of_replying: 'Acted where the incumbent replied',
  both_replied: 'Both replied',
  not_compared: 'Could not be compared',
};

const FINDING_COPY: Record<string, string> = {
  unknown_tool: 'Called a tool that does not exist',
  invalid_args: 'Arguments the tool rejects',
  unrequested_mutation: 'Reached for a mutating tool the incumbent did not',
  truncated: 'Response truncated mid-thought',
  call_failed: 'Provider call failed',
};

const VERDICT_COPY: Record<string, string> = {
  equivalent: 'Judge: equivalent',
  candidate_better: 'Judge: candidate better',
  candidate_worse: 'Judge: candidate worse',
  candidate_unsafe: 'Judge: candidate unsafe',
  judge_failed: 'Judge: unavailable',
};

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function money(totals: { total_cost_usd: string; pricing_available: boolean }): string {
  if (!totals.pricing_available) return 'unknown';
  return `$${Number(totals.total_cost_usd).toFixed(4)}`;
}

function ms(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

// ---------------------------------------------------------------------------
// Report pieces
// ---------------------------------------------------------------------------

function VerdictBanner({ summary, userId }: { summary: EvalSummary; userId: string }) {
  const copy = RECOMMENDATION_COPY[summary.recommendation] ?? RECOMMENDATION_COPY.inconclusive;
  return (
    <div className={`rounded-[--radius-lg] border p-4 ${copy.className}`}>
      <p className="text-lg font-semibold">{copy.label}</p>
      <ul className="mt-2 space-y-1 text-sm">
        {summary.reasons.map(reason => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      {/* Links out rather than applying the switch here. The per-user override
          already has one owner (user detail -> LLM), and a second control
          writing the same column is how the two drift. The wording stays
          neutral across verdicts: this is also the page you visit to clear an
          override, and a report saying "do not switch" should not be handing
          out a button that reads like permission. */}
      <Link
        to={`${adminPath('users')}/${userId}/llm`}
        className="mt-3 inline-block text-sm font-medium underline underline-offset-2"
      >
        Model settings for this user
      </Link>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-[--radius-md] border border-border bg-card p-3">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-1 text-xl font-semibold text-foreground">{value}</p>
      {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

function SummaryGrid({ summary }: { summary: EvalSummary }) {
  const safetyTotal = summary.blocking_findings;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Stat
        label="Safety findings"
        value={String(safetyTotal)}
        hint={safetyTotal ? 'Any finding blocks a switch' : 'None across the run'}
      />
      <Stat
        label="Matched the incumbent"
        value={pct(summary.identical_rate)}
        hint={`${summary.turns_completed} turns compared`}
      />
      <Stat
        label="Replied instead of acting"
        value={pct(summary.silent_noop_rate)}
        hint="Turns the incumbent acted on"
      />
      <Stat
        label="Cost per run"
        value={money(summary.candidate)}
        hint={`Incumbent ${money(summary.baseline)}`}
      />
      <Stat
        label="Candidate latency (p95)"
        value={ms(summary.candidate.latency_p95_ms)}
        hint={`Incumbent ${ms(summary.baseline.latency_p95_ms)}`}
      />
      <Stat
        label="Candidate cache reads"
        value={pct(summary.candidate.cache_read_ratio)}
        hint={`Incumbent ${pct(summary.baseline.cache_read_ratio)}`}
      />
      <Stat label="Turns that diverged" value={pct(summary.divergence_rate)} />
      <Stat
        label="Turns that failed"
        value={String(summary.turns_failed)}
        hint="Could not be compared"
      />
    </div>
  );
}

function DecisionColumn({ title, decision }: { title: string; decision: EvalDecision }) {
  return (
    <div className="min-w-0 flex-1">
      <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      {decision.error ? (
        <p className="text-sm text-error-text">{decision.error}</p>
      ) : (
        <>
          {decision.tool_calls.length > 0 ? (
            <ul className="mb-2 space-y-1">
              {decision.tool_calls.map((call, index) => (
                <li
                  key={`${call.name}-${index}`}
                  className="rounded-[--radius-sm] bg-panel px-2 py-1 font-mono text-xs text-foreground"
                >
                  <span className="font-semibold">{call.name}</span>
                  <span className="text-muted-foreground">
                    ({JSON.stringify(call.arguments)})
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mb-2 text-xs italic text-muted-foreground">No tool calls</p>
          )}
          {decision.text ? (
            <p className="whitespace-pre-wrap text-sm text-foreground">{decision.text}</p>
          ) : null}
        </>
      )}
    </div>
  );
}

function TurnCard({ turn }: { turn: EvalTurn }) {
  const [open, setOpen] = useState(turn.safety_issues.length > 0);
  return (
    <div className="rounded-[--radius-md] border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 p-3 text-left"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-foreground">{turn.user_message}</p>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground">
              {AGREEMENT_COPY[turn.agreement] ?? turn.agreement}
            </span>
            {turn.safety_issues.map((issue, index) => (
              <span
                key={`${issue.finding}-${index}`}
                className="rounded-full bg-error-bg px-2 py-0.5 text-error-text"
              >
                {FINDING_COPY[issue.finding] ?? issue.finding}
                {issue.tool_name ? `: ${issue.tool_name}` : ''}
              </span>
            ))}
            {turn.judge_verdict !== 'not_judged' ? (
              <span className="rounded-full bg-panel px-2 py-0.5 text-muted-foreground">
                {VERDICT_COPY[turn.judge_verdict] ?? turn.judge_verdict}
              </span>
            ) : null}
          </div>
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">{open ? 'Hide' : 'Compare'}</span>
      </button>

      {open ? (
        <div className="border-t border-border p-3">
          <div className="flex flex-col gap-4 sm:flex-row">
            <DecisionColumn title="Incumbent" decision={turn.baseline} />
            <DecisionColumn title="Candidate" decision={turn.candidate} />
          </div>
          {turn.judge_rationale ? (
            <p className="mt-3 border-t border-border pt-3 text-sm text-muted-foreground">
              {turn.judge_rationale}
            </p>
          ) : null}
          {turn.historic_reply ? (
            <details className="mt-3 border-t border-border pt-3">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                What the agent actually replied at the time
              </summary>
              <p className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground">
                {turn.historic_reply}
              </p>
              {turn.historic_tool_names.length > 0 ? (
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {turn.historic_tool_names.join(', ')}
                </p>
              ) : null}
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ModelEvalTab() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const [userId, setUserId] = useState('');

  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [sampleCount, setSampleCount] = useState(100);
  const [judgeEnabled, setJudgeEnabled] = useState(true);

  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [report, setReport] = useState<EvalReport | null>(null);
  const [turnLimit, setTurnLimit] = useState(TURN_PAGE_SIZE);

  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only consenting users are evaluable, so the picker never offers a user
  // whose run would 403.
  useEffect(() => {
    setUsersLoading(true);
    getAdminUsers({ consent: 'shared', limit: 200 })
      .then(res => setUsers(res.items))
      .catch((e: Error) => setError(e.message))
      .finally(() => setUsersLoading(false));
  }, []);

  const refreshRuns = useCallback(async (id: string) => {
    if (!id) {
      setRuns([]);
      return;
    }
    try {
      setRuns(await listEvalRuns(id));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    setSelectedRunId(null);
    setReport(null);
    setTurnLimit(TURN_PAGE_SIZE);
    void refreshRuns(userId);
  }, [userId, refreshRuns]);

  const activeRun = runs.find(r => ACTIVE_STATUSES.has(r.status));

  // One interval, restarted whenever what we are watching changes. While a run
  // is active we re-read the list so progress advances; once it settles the
  // interval clears itself rather than polling a finished run forever.
  const activeRunId = activeRun?.id ?? null;
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (activeRunId == null || !userId) return;
    pollRef.current = setInterval(() => void refreshRuns(userId), POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [activeRunId, userId, refreshRuns]);

  // Re-fetch as the selected run advances, not only when the selection
  // changes. A run selected while it is still pending has no summary yet, and
  // without the status in the dependencies the report would sit on "no
  // summary" for good while the row beside it said completed.
  const selectedStatus = runs.find(r => r.id === selectedRunId)?.status;
  useEffect(() => {
    if (selectedRunId == null) {
      setReport(null);
      return;
    }
    getEvalReport(selectedRunId, turnLimit)
      .then(setReport)
      .catch((e: Error) => setError(e.message));
  }, [selectedRunId, selectedStatus, turnLimit]);

  async function handleStart() {
    setError(null);
    setStarting(true);
    try {
      const run = await startEvalRun(userId, {
        candidateProvider: provider,
        candidateModel: model,
        sampleCount,
        judgeEnabled,
      });
      setRuns(prev => [run, ...prev]);
      setSelectedRunId(run.id);
      setTurnLimit(TURN_PAGE_SIZE);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  async function handleCancel(runId: number) {
    try {
      await cancelEvalRun(runId);
      await refreshRuns(userId);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const canStart = Boolean(userId && provider && model) && !starting && !activeRun;

  return (
    <div className="space-y-6">
      {error ? (
        <div className="rounded-[--radius-md] bg-error-bg px-3 py-2 text-sm text-error-text">
          {error}
        </div>
      ) : null}

      {/* Run configuration */}
      <section className="rounded-[--radius-lg] border border-border bg-card p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
          <label className="block">
            <span className="mb-1 block text-sm text-muted-foreground">User</span>
            <select
              value={userId}
              onChange={e => setUserId(e.target.value)}
              disabled={usersLoading}
              className="w-full rounded-[--radius-md] border border-border bg-card px-3 py-2 text-sm text-foreground"
            >
              <option value="">
                {usersLoading ? 'Loading users...' : 'Select a consenting user'}
              </option>
              {users.map(u => (
                <option key={u.id} value={u.id}>
                  {u.email || u.user_id}
                </option>
              ))}
            </select>
          </label>

          {/* Both pickers need an explicit empty option. Without one the
              <select> renders its first real entry as the visible choice while
              this component's state is still empty, so the form looks filled
              in, the model list stays on "pick a provider first", and the Run
              button is disabled for no reason the operator can see. */}
          <label className="block">
            <span className="mb-1 block text-sm text-muted-foreground">Candidate provider</span>
            <LLMProviderSelect
              value={provider}
              onChange={next => {
                setProvider(next);
                // A model id is only meaningful for the provider it came from.
                setModel('');
              }}
              allowEmpty
              emptyLabel="Select a provider"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-muted-foreground">Candidate model</span>
            <LLMModelSelect
              provider={provider}
              value={model}
              onChange={setModel}
              allowEmpty
              emptyLabel="Select a model"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-muted-foreground">Turns to replay</span>
            <select
              value={sampleCount}
              onChange={e => setSampleCount(Number(e.target.value))}
              className="w-full rounded-[--radius-md] border border-border bg-card px-3 py-2 text-sm text-foreground"
            >
              {SAMPLE_CHOICES.map(n => (
                <option key={n} value={n}>
                  Most recent {n}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={judgeEnabled}
              onChange={e => setJudgeEnabled(e.target.checked)}
            />
            Adjudicate divergences with the incumbent model
          </label>
          <button
            type="button"
            onClick={() => void handleStart()}
            disabled={!canStart}
            className="rounded-[--radius-md] bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {starting ? 'Starting...' : 'Run analysis'}
          </button>
          {activeRun ? (
            <span className="text-sm text-muted-foreground">
              An evaluation is already running for this user.
            </span>
          ) : null}
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Each turn is sent to both models and no tool is ever executed, so a run cannot message
          anyone or change any record. Replays use the current system prompt and tool set, not the
          ones in force when the turn happened.
        </p>
      </section>

      {/* In-flight progress */}
      {activeRun ? (
        <section className="rounded-[--radius-lg] border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-foreground">
              Replaying {activeRun.progress_completed} of {activeRun.progress_total || '?'} turns
              against {activeRun.candidate_model}
            </p>
            <button
              type="button"
              onClick={() => void handleCancel(activeRun.id)}
              className="rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
            >
              Cancel
            </button>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-panel">
            <div
              className="h-full bg-primary transition-all"
              style={{
                width: activeRun.progress_total
                  ? `${(activeRun.progress_completed / activeRun.progress_total) * 100}%`
                  : '5%',
              }}
            />
          </div>
        </section>
      ) : null}

      {/* Past runs */}
      {runs.length > 0 ? (
        <section className="rounded-[--radius-lg] border border-border bg-card">
          <h3 className="border-b border-border p-3 text-sm font-semibold text-foreground">
            Runs for this user
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="p-3">Started</th>
                  <th className="p-3">Candidate</th>
                  <th className="p-3">Turns</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {runs.map(run => {
                  const copy = RECOMMENDATION_COPY[run.recommendation as EvalRecommendation];
                  return (
                    <tr
                      key={run.id}
                      onClick={() => {
                        setSelectedRunId(run.id);
                        setTurnLimit(TURN_PAGE_SIZE);
                      }}
                      className={`cursor-pointer border-t border-border ${
                        selectedRunId === run.id ? 'bg-panel' : ''
                      }`}
                    >
                      <td className="p-3 text-muted-foreground">
                        {formatRelative(run.created_at)}
                      </td>
                      <td className="p-3 font-mono text-xs text-foreground">
                        {run.candidate_model}
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {run.progress_total || run.requested_samples}
                      </td>
                      <td className="p-3 text-muted-foreground">{run.status}</td>
                      <td className="p-3">
                        {copy ? (
                          <span className={`rounded-full px-2 py-0.5 text-xs ${copy.className}`}>
                            {copy.label}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">-</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {/* Report */}
      {report?.run.summary ? (
        <section className="space-y-4">
          <VerdictBanner summary={report.run.summary} userId={report.run.user_id} />

          {report.run.summary.warnings.map(warning => (
            <div
              key={warning}
              className="rounded-[--radius-md] bg-warning-bg px-3 py-2 text-sm text-warning-text"
            >
              {warning}
            </div>
          ))}

          <SummaryGrid summary={report.run.summary} />

          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              Turns, most concerning first
              {report.total_turns > report.turns.length
                ? ` (${report.turns.length} of ${report.total_turns})`
                : ''}
            </h3>
            <div className="space-y-2">
              {report.turns.map(turn => (
                <TurnCard key={turn.message_seq} turn={turn} />
              ))}
            </div>
            {report.total_turns > report.turns.length ? (
              <button
                type="button"
                onClick={() => setTurnLimit(n => n + TURN_PAGE_SIZE)}
                className="mt-2 rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
              >
                Show more turns
              </button>
            ) : null}
          </div>
        </section>
      ) : null}

      {report && !report.run.summary ? (
        <p className="text-sm text-muted-foreground">
          This run has no summary{report.run.error ? `: ${report.run.error}` : '.'}
        </p>
      ) : null}
    </div>
  );
}
