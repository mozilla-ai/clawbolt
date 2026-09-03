import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
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
// Row pieces, shared by the two layouts
// ---------------------------------------------------------------------------

/** The run's verdict, or *empty* when it has none.
 *
 * The table passes a dash: a blank cell in a column of pills reads as a
 * rendering fault. A card has no column to keep aligned, so it passes
 * nothing and lets the status line carry "running" or "cancelled".
 */
function VerdictPill({ run, empty = null }: { run: EvalRun; empty?: ReactNode }) {
  const copy = RECOMMENDATION_COPY[run.recommendation as EvalRecommendation];
  if (!copy) return <>{empty}</>;
  return <span className={`rounded-full px-2 py-0.5 text-xs ${copy.className}`}>{copy.label}</span>;
}

/** Withheld while the run is in flight, since the API refuses to delete an
 *  active run and wants it cancelled first.
 *
 * Present on every settled run, including one whose user withdrew consent:
 * its report can no longer be opened, which makes it the row most worth
 * clearing out.
 */
function DeleteRunButton({ run, onPick }: { run: EvalRun; onPick: (run: EvalRun) => void }) {
  if (ACTIVE_STATUSES.has(run.status)) return null;
  return (
    <button
      type="button"
      aria-label={`Delete run against ${run.candidate_model}`}
      onClick={e => {
        // The row and the card both navigate to the report.
        e.stopPropagation();
        onPick(run);
      }}
      // A 44px target on the card, compact in the table row. DESIGN.md sets
      // the density for a phone held in gloves, and on the card this button
      // sits inside a tap area that navigates to the report.
      className="min-h-11 shrink-0 rounded-[--radius-md] border border-border px-3 text-xs text-muted-foreground hover:bg-error-bg hover:text-error-text xl:min-h-0 xl:px-2 xl:py-1"
    >
      Delete
    </button>
  );
}

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
          <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between">
            <p className="min-w-0 break-words text-sm text-foreground">
              Replaying {activeRun.progress_completed} of {activeRun.progress_total || '?'} turns
              against {activeRun.candidate_model}
            </p>
            <Link
              to={`${adminPath('model-eval')}/${activeRun.id}`}
              className="shrink-0 rounded-[--radius-md] border border-border px-3 py-1 text-sm text-muted-foreground"
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
          <>
            {/* Cards up to the width where the table below fits. Eight
                columns of it measure 990px, and the sidebar leaves 750px at
                1024px of viewport, so the switch is ``xl``: anything narrower
                showed Started and User and hid the verdict and the delete
                control behind a sideways scroll inside the card, which is the
                same complaint on a laptop as on a phone. Two-up from ``sm``,
                since one column of cards across a 1200px window is mostly
                empty space.

                Both layouts sit in the DOM and CSS picks one. Choosing in JS
                would need a media query, which is only readable after mount,
                so the first paint would show the wrong one and then jump. */}
            <ul
              aria-label="Evaluation runs"
              // ``grid-cols-1`` is not redundant. Without an explicit track
              // the single implicit column is sized to max-content, and the
              // card's nowrap user line then sets the card's width instead of
              // the reverse: ``truncate`` never fires, the card grows past
              // the viewport, and the delete control ends up off-screen. At
              // 320px with a 47-character email that was a 420px card in a
              // 320px window. ``main`` absorbs the overflow, so the document
              // width stays honest and only the scroller shows it.
              className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 xl:hidden"
            >
              {runs.map(run => {
                const href = `${adminPath('model-eval')}/${run.id}`;
                return (
                  <li
                    key={run.id}
                    onClick={() => {
                      // A run whose user withdrew consent has no readable
                      // report, so the card is not a way into one.
                      if (run.user_consented) navigate(href);
                    }}
                    className={`space-y-1 rounded-[--radius-md] border border-border p-3 ${
                      run.user_consented ? 'cursor-pointer hover:bg-panel' : 'opacity-60'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      {run.user_consented ? (
                        <Link
                          to={href}
                          className="text-sm text-primary hover:underline"
                          onClick={e => e.stopPropagation()}
                        >
                          {formatRelative(run.created_at)}
                        </Link>
                      ) : (
                        <span className="text-sm text-muted-foreground">
                          {formatRelative(run.created_at)}
                        </span>
                      )}
                      <VerdictPill run={run} />
                    </div>
                    {/* ``break-all`` rather than a truncation: a
                        gateway-qualified model id is one unbroken 40-character
                        token whose tail is the part that identifies it, and it
                        is the reason an operator opened this list. */}
                    <p className="break-all font-mono text-xs text-foreground">
                      {run.candidate_model}
                    </p>
                    <p className="break-all font-mono text-xs text-muted-foreground">
                      against {run.baseline_model}
                    </p>
                    <div className="flex items-end justify-between gap-2">
                      <div className="min-w-0 text-xs text-muted-foreground">
                        {userId ? null : (
                          <>
                            <p className="truncate">{run.user_email || run.user_id}</p>
                            {run.user_consented ? null : (
                              <p className="text-warning-text">consent withdrawn</p>
                            )}
                          </>
                        )}
                        <p>
                          {run.status} | {run.progress_total || run.requested_samples} turns
                        </p>
                      </div>
                      <DeleteRunButton run={run} onPick={setDeleteTarget} />
                    </div>
                  </li>
                );
              })}
            </ul>

            {/* ``relative`` is load-bearing, not decoration. The header's
                ``sr-only`` span is absolutely positioned, so without a
                positioned ancestor its containing block is the viewport
                rather than this scroller. It then escapes the clip, sits at
                the far edge of the table, and stretches the document to
                match, at which point the browser renders the whole page
                zoomed out to fit. Nothing else about the page looks wrong,
                which is what made it hard to find.

                It still matters at the widths this table is shown at, not
                only at the phone widths that first surfaced it: whenever the
                table exceeds this scroller, dropping the class stretches the
                document. Measured at 1280px with a long user email, where the
                table wants 1112px in a 1006px box. */}
            <div className="relative hidden overflow-x-auto xl:block">
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
                    const href = `${adminPath('model-eval')}/${run.id}`;
                    return (
                      <tr
                        key={run.id}
                        onClick={() => {
                          if (run.user_consented) navigate(href);
                        }}
                        className={`border-t border-border ${
                          run.user_consented ? 'cursor-pointer hover:bg-panel' : 'opacity-60'
                        }`}
                      >
                        <td className="whitespace-nowrap p-3 text-muted-foreground">
                          {run.user_consented ? (
                            // A real link, so the row is keyboard reachable
                            // and its URL is copyable.
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
                        {/* Wrapping, unlike every other column. A
                            gateway-qualified model id runs past 40 characters
                            and two of them held on one line pushed Verdict
                            and the delete control off the right edge of the
                            card at 1440px, where the only way to reach them
                            was to scroll the table sideways. */}
                        <td className="break-all p-3 font-mono text-xs text-foreground">
                          {run.candidate_model}
                        </td>
                        <td className="break-all p-3 font-mono text-xs text-muted-foreground">
                          {run.baseline_model}
                        </td>
                        <td className="p-3 text-muted-foreground">
                          {run.progress_total || run.requested_samples}
                        </td>
                        <td className="whitespace-nowrap p-3 text-muted-foreground">
                          {run.status}
                        </td>
                        <td className="whitespace-nowrap p-3">
                          <VerdictPill
                            run={run}
                            empty={<span className="text-muted-foreground">-</span>}
                          />
                        </td>
                        <td className="whitespace-nowrap p-3 text-right">
                          <DeleteRunButton run={run} onPick={setDeleteTarget} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
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
