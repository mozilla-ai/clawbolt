import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  getAdminUsers,
  listEvalRuns,
  startEvalRun,
  type AdminUser,
  type EvalRecommendation,
  type EvalRun,
} from '../admin-api';
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
      const list = await listEvalRuns(id);
      setRuns(list.runs);
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
                      onClick={() => navigate(`${adminPath('model-eval')}/${run.id}`)}
                      className="cursor-pointer border-t border-border hover:bg-panel"
                    >
                      <td className="p-3 text-muted-foreground">
                        {/* A real link in the first cell, so the row is
                            keyboard reachable and its URL is copyable, while
                            the row click stays the large target. */}
                        <Link
                          to={`${adminPath('model-eval')}/${run.id}`}
                          className="text-primary hover:underline"
                          onClick={e => e.stopPropagation()}
                        >
                          {formatRelative(run.created_at)}
                        </Link>
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

    </div>
  );
}
