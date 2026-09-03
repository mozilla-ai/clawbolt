import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  deleteEvalRun,
  getAdminUsers,
  listEvalRuns,
  startEvalRun,
  type AdminUser,
  type EvalRecommendation,
  type EvalRun,
} from '../admin-api';
import ConfirmDialog from '../ConfirmDialog';
import { LLMProviderSelect, LLMModelSelect } from '../llm-picker';
import { formatRelative } from '../format';
import { adminPath } from '../nav-items';
import { ACTIVE_STATUSES, POLL_MS, RECOMMENDATION_COPY } from './model-eval-common';

// Model comparison: "can I move this user to a different model without
// breaking them". A run replays the user's own recent turns through their
// current model and a candidate and records what each decided.
//
// This page starts runs and lists them. A run's evidence lives at its own URL
// (``model-eval/<public id>``, rendered by ``model-eval-report.tsx``), because
// a report is read long after the run that produced it and is often read by
// someone who did not start it.
//
// Only users who opted into data sharing can be evaluated: a run reads their
// real conversations and the report renders them back. The picker is filtered
// to consenting users so the 403 is never reachable from the UI.

// Slider bounds. The ceiling comes from the API (LLM_EVAL_MAX_SAMPLES), so a
// deployment that lowers the setting cannot be offered a value start_run would
// reject. These constants are the fallback until the first list call answers,
// plus the floor and step, which are the UI's own.
const SAMPLE_MIN = 5;
const SAMPLE_STEP = 5;
const SAMPLE_MAX_FALLBACK = 200;
const SAMPLE_DEFAULT = 100;

// Rows per page in the run table. Each row is one run's metadata, so this is
// about scanning, not cost.
const RUN_PAGE_SIZE = 25;


// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ModelEvalTab() {
  const navigate = useNavigate();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const [userId, setUserId] = useState('');

  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [sampleCount, setSampleCount] = useState(SAMPLE_DEFAULT);
  const [sampleMax, setSampleMax] = useState(SAMPLE_MAX_FALLBACK);
  const [minTurnsForVerdict, setMinTurnsForVerdict] = useState(0);
  const [judgeEnabled, setJudgeEnabled] = useState(true);

  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [runTotal, setRunTotal] = useState(0);
  const [runsShown, setRunsShown] = useState(RUN_PAGE_SIZE);

  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The run the confirm dialog is asking about, or null when it is closed.
  // Holding the row rather than a bare id lets the dialog name what it is
  // about to destroy, which is the only thing making the gate meaningful.
  const [deleteTarget, setDeleteTarget] = useState<EvalRun | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Only consenting users are evaluable, so the picker never offers a user
  // whose run would 403.
  useEffect(() => {
    setUsersLoading(true);
    getAdminUsers({ consent: 'shared', limit: 200 })
      .then(res => setUsers(res.items))
      .catch((e: Error) => setError(e.message))
      .finally(() => setUsersLoading(false));
  }, []);

  // Unfiltered when no user is picked: the table's job is answering "what has
  // been evaluated lately" so a run can be found again without remembering
  // whose it was. Picking a user in the form above narrows it.
  const refreshRuns = useCallback(async (id: string, limit: number) => {
    try {
      const list = await listEvalRuns({ userId: id || undefined, limit });
      setRuns(list.runs);
      setRunTotal(list.total);
      setSampleMax(list.max_samples);
      setMinTurnsForVerdict(list.min_turns_for_verdict);
      // A cap below the current selection would leave the thumb pinned past
      // the end of its own track and start a run the API rejects.
      setSampleCount(prev => Math.min(prev, list.max_samples));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refreshRuns(userId, runsShown);
  }, [userId, runsShown, refreshRuns]);

  // Scoped to the selected user on purpose. ``start_run`` allows one active
  // run per user, so another tenant's run in the unfiltered list must not
  // disable this form.
  const activeRun = runs.find(
    r => ACTIVE_STATUSES.has(r.status) && (!userId || r.user_id === userId),
  );

  // One interval, restarted whenever what we are watching changes. While a run
  // is active we re-read the list so progress advances; once it settles the
  // interval clears itself rather than polling a finished run forever.
  const activeRunId = activeRun?.id ?? null;
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (activeRunId == null || !userId) return;
    pollRef.current = setInterval(() => void refreshRuns(userId, runsShown), POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [activeRunId, userId, runsShown, refreshRuns]);

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
      navigate(`${adminPath('model-eval')}/${run.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteEvalRun(deleteTarget.id);
      // Drop the row locally rather than refetching: the list may be paged
      // several clicks deep and a reload would snap it back to the first page.
      setRuns(prev => prev.filter(r => r.id !== deleteTarget.id));
      setRunTotal(n => Math.max(0, n - 1));
      setDeleteTarget(null);
    } catch (e) {
      // Close the dialog rather than leaving it open over the banner. The
      // likely failure is the 409 telling the operator to cancel the run
      // first, and that is acted on out here, not in the dialog.
      setError((e as Error).message);
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
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
            <span className="mb-1 flex items-baseline justify-between text-sm text-muted-foreground">
              <span>Turns to replay</span>
              <span className="font-medium text-foreground">Most recent {sampleCount}</span>
            </span>
            <input
              type="range"
              min={SAMPLE_MIN}
              max={sampleMax}
              step={SAMPLE_STEP}
              value={sampleCount}
              onChange={e => setSampleCount(Number(e.target.value))}
              aria-label="Turns to replay"
              aria-valuetext={`Most recent ${sampleCount} turns`}
              // The native control, themed through ``accent-color``. An
              // earlier version set ``appearance-none`` with a token
              // background, which leaves Chromium drawing its own track on
              // top: invisible against a light page, a white slab in dark
              // mode. Styling every vendor pseudo-element is the alternative,
              // and it buys nothing here.
              className="mt-1 w-full cursor-pointer accent-primary"
            />
            {/* Endpoints only, with the middle left empty until there is
                something worth saying there: in a four-column grid this cell
                is narrow, and a permanent hint between the bounds wraps and
                crowds them. */}
            <span className="mt-1 flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
              <span>{SAMPLE_MIN}</span>
              {minTurnsForVerdict > 0 && sampleCount < minTurnsForVerdict ? (
                <span className="text-warning-text">
                  Under {minTurnsForVerdict} reports inconclusive
                </span>
              ) : null}
              <span>{sampleMax}</span>
            </span>
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

      {/* In-flight progress. Deliberately still here rather than only on the
          run's page: an operator arriving from elsewhere needs to see that
          this user already has a run going before they try to start one. */}
      {activeRun ? (
        <section className="rounded-[--radius-lg] border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-foreground">
              Replaying {activeRun.progress_completed} of {activeRun.progress_total || '?'} turns
              against {activeRun.candidate_model}
            </p>
            <Link
              to={`${adminPath('model-eval')}/${activeRun.id}`}
              className="rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
            >
              Open report
            </Link>
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

      {/* The run browser. Present whether or not a user is selected: with no
          selection it is every run, newest first, which is how a run gets
          found again weeks later. */}
      <section className="rounded-[--radius-lg] border border-border bg-card">
        <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border p-3">
          <h3 className="text-sm font-semibold text-foreground">
            {userId ? 'Runs for this user' : 'Recent evaluations'}
          </h3>
          <p className="text-xs text-muted-foreground">
            {runTotal > runs.length
              ? `${runs.length} of ${runTotal}`
              : `${runTotal} run${runTotal === 1 ? '' : 's'}`}
          </p>
        </div>
        {runs.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">
            {userId
              ? 'No evaluations for this user yet.'
              : 'No evaluations yet. Pick a user above to run the first one.'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="p-3">Started</th>
                  {userId ? null : <th className="p-3">User</th>}
                  <th className="p-3">Candidate</th>
                  <th className="p-3">Incumbent</th>
                  <th className="p-3">Turns</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Verdict</th>
                  <th className="p-3">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.map(run => {
                  const copy = RECOMMENDATION_COPY[run.recommendation as EvalRecommendation];
                  const href = `${adminPath('model-eval')}/${run.id}`;
                  return (
                    <tr
                      key={run.id}
                      onClick={() => {
                        // A run whose user withdrew consent has no readable
                        // report, so the row is not a way into one.
                        if (run.user_consented) navigate(href);
                      }}
                      className={`border-t border-border ${
                        run.user_consented ? 'cursor-pointer hover:bg-panel' : 'opacity-60'
                      }`}
                    >
                      <td className="whitespace-nowrap p-3 text-muted-foreground">
                        {run.user_consented ? (
                          // A real link, so the row is keyboard reachable and
                          // its URL is copyable.
                          <Link
                            to={href}
                            className="text-primary hover:underline"
                            onClick={e => e.stopPropagation()}
                          >
                            {formatRelative(run.created_at)}
                          </Link>
                        ) : (
                          formatRelative(run.created_at)
                        )}
                      </td>
                      {userId ? null : (
                        <td className="whitespace-nowrap p-3 text-muted-foreground">
                          {run.user_email || run.user_id}
                          {run.user_consented ? null : (
                            <span className="ml-2 text-xs text-warning-text">
                              consent withdrawn
                            </span>
                          )}
                        </td>
                      )}
                      <td className="whitespace-nowrap p-3 font-mono text-xs text-foreground">
                        {run.candidate_model}
                      </td>
                      <td className="whitespace-nowrap p-3 font-mono text-xs text-muted-foreground">
                        {run.baseline_model}
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {run.progress_total || run.requested_samples}
                      </td>
                      <td className="whitespace-nowrap p-3 text-muted-foreground">{run.status}</td>
                      <td className="whitespace-nowrap p-3">
                        {copy ? (
                          <span className={`rounded-full px-2 py-0.5 text-xs ${copy.className}`}>
                            {copy.label}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">-</span>
                        )}
                      </td>
                      {/* Present on every row, including a withdrawn-consent
                          one whose report cannot be opened: that is the row
                          most worth clearing out. Withheld only while the run
                          is in flight, since the API wants it cancelled
                          first. */}
                      <td className="whitespace-nowrap p-3 text-right">
                        {ACTIVE_STATUSES.has(run.status) ? null : (
                          <button
                            type="button"
                            aria-label={`Delete run against ${run.candidate_model}`}
                            onClick={e => {
                              // The row itself navigates to the report.
                              e.stopPropagation();
                              setDeleteTarget(run);
                            }}
                            className="rounded-[--radius-md] border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-error-bg hover:text-error-text"
                          >
                            Delete
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {runTotal > runs.length ? (
          <div className="border-t border-border p-3">
            <button
              type="button"
              onClick={() => setRunsShown(n => n + RUN_PAGE_SIZE)}
              className="rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
            >
              Show more runs
            </button>
          </div>
        ) : null}
      </section>

      <ConfirmDialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
        title="Delete this evaluation run?"
        description={
          <div className="space-y-2">
            <p>
              This removes the run and every turn of evidence recorded under it. It cannot be
              undone, and the replay would have to be paid for again to get it back.
            </p>
            <p className="text-xs">
              {deleteTarget?.candidate_model} against {deleteTarget?.baseline_model}, started{' '}
              {deleteTarget ? formatRelative(deleteTarget.created_at) : ''}.
            </p>
          </div>
        }
        confirmLabel="Delete run"
        destructive
        busy={deleting}
      />
    </div>
  );
}
