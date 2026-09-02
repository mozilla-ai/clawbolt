import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  cancelEvalRun,
  getEvalReport,
  type EvalDecision,
  type EvalReport,
  type EvalSummary,
  type EvalTurn,
} from '../admin-api';
import { formatRelative } from '../format';
import { adminPath } from '../nav-items';
import { ACTIVE_STATUSES, POLL_MS, RECOMMENDATION_COPY } from './model-eval-common';

// One evaluation run's report, at its own URL under the run's public id.
//
// It is a page rather than a panel on the run form because of what an
// operator does with it: they read it, leave, come back to it days later
// when the switch is actually being decided, and paste the link to someone
// else. A run is also long enough that the person who reads the report is
// often not at the machine that started it.
//
// Three things drive the layout:
//
// - The verdict is stated once, in words, at the top. Someone opening this
//   page has one question, and a grid of rates does not answer it.
// - Safety findings are never mixed into a quality score. They are listed
//   separately and they are what sinks a recommendation, because each one is
//   an action the agent loop would have taken against a real account.
// - The turn drill-down is the point, not an appendix. Numbers persuade
//   nobody about a decision this consequential; reading six diverging turns
//   does. The report arrives worst-first so the turns that matter are the
//   ones on screen.
//
// A run still in flight renders here too, with its progress and whatever
// turns have landed, so the page is worth opening before the run finishes.

// Turns are returned worst-first, so the first page is the part that decides
// anything. The rest is available on request rather than shipped by default:
// every text column on a turn is decrypted and PII-scrubbed per request.
const TURN_PAGE_SIZE = 50;


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

export default function ModelEvalReportPage({ runId }: { runId: string }) {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [turnLimit, setTurnLimit] = useState(TURN_PAGE_SIZE);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const load = useCallback(
    async (limit: number) => {
      try {
        setReport(await getEvalReport(runId, limit));
      } catch (e) {
        const message = (e as Error).message;
        // A bad id in a pasted link is the common case here, and it deserves
        // a way back to the run list rather than a red banner.
        if (/not found/i.test(message)) setNotFound(true);
        else setError(message);
      }
    },
    [runId],
  );

  useEffect(() => {
    setReport(null);
    setNotFound(false);
    setError(null);
    void load(turnLimit);
  }, [load, turnLimit]);

  // Poll only while the run is unfinished, and stop as soon as it settles: a
  // report is immutable once the run completes, so polling it forever writes
  // an audit row every two seconds for nothing.
  const isActive = report ? ACTIVE_STATUSES.has(report.run.status) : false;
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!isActive) return;
    pollRef.current = setInterval(() => void load(turnLimit), POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [isActive, load, turnLimit]);

  async function handleCancel() {
    try {
      await cancelEvalRun(runId);
      await load(turnLimit);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const backLink = (
    <Link to={adminPath('model-eval')} className="text-sm text-primary hover:underline">
      Back to model comparison
    </Link>
  );

  if (notFound) {
    return (
      <div className="space-y-2 text-sm">
        <p className="text-danger">That evaluation run does not exist.</p>
        {backLink}
      </div>
    );
  }

  if (!report) {
    return (
      <div className="space-y-3">
        {backLink}
        <div className="h-32 animate-pulse rounded-[--radius-md] bg-panel" />
      </div>
    );
  }

  const { run } = report;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        {backLink}
        <p className="text-xs text-muted-foreground">
          {run.candidate_model} against {run.baseline_model}, started{' '}
          {formatRelative(run.created_at)}
        </p>
      </div>

      {error ? (
        <div className="rounded-[--radius-md] bg-error-bg px-3 py-2 text-sm text-error-text">
          {error}
        </div>
      ) : null}

      {isActive ? (
        <section className="rounded-[--radius-lg] border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-foreground">
              Replaying {run.progress_completed} of {run.progress_total || '?'} turns against{' '}
              {run.candidate_model}
            </p>
            <button
              type="button"
              onClick={() => void handleCancel()}
              className="rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
            >
              Cancel
            </button>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-panel">
            <div
              className="h-full bg-primary transition-all"
              style={{
                width: run.progress_total
                  ? `${(run.progress_completed / run.progress_total) * 100}%`
                  : '5%',
              }}
            />
          </div>
        </section>
      ) : null}

      {run.summary ? (
        <>
          <VerdictBanner summary={run.summary} userId={run.user_id} />

          {run.summary.warnings.map(warning => (
            <div
              key={warning}
              className="rounded-[--radius-md] bg-warning-bg px-3 py-2 text-sm text-warning-text"
            >
              {warning}
            </div>
          ))}

          <SummaryGrid summary={run.summary} />
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          {isActive
            ? 'The verdict is written when the run finishes. Turns appear below as they land.'
            : `This run has no summary${run.error ? `: ${run.error}` : '.'}`}
        </p>
      )}

      {report.turns.length > 0 ? (
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
      ) : null}
    </div>
  );
}
