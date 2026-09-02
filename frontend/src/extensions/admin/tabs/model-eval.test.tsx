import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { EvalReport, EvalRun, EvalSummary, EvalTurn } from '../admin-api';
import ModelEvalTab from './model-eval';

// ---------------------------------------------------------------------------
// Admin Model Eval tab.
//
// The tab exists to answer one question honestly, so the tests care most
// about the ways it could answer it dishonestly: offering a user who cannot
// legally be evaluated, showing an unpriced model's cost as free, burying a
// safety finding below a hundred matched turns, or letting a second run start
// while one is in flight.
// ---------------------------------------------------------------------------

vi.mock('../admin-api', () => ({
  getAdminUsers: vi.fn(),
  listEvalRuns: vi.fn(),
  startEvalRun: vi.fn(),
  getEvalReport: vi.fn(),
  cancelEvalRun: vi.fn(),
}));

vi.mock('../llm-picker', () => ({
  LLMProviderSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input aria-label="provider" value={value} onChange={e => onChange(e.target.value)} />
  ),
  LLMModelSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input aria-label="model" value={value} onChange={e => onChange(e.target.value)} />
  ),
}));

const USERS = {
  total: 1,
  skip: 0,
  limit: 200,
  items: [
    {
      id: 'user-1',
      user_id: 'google_abc',
      email: 'consenting@example.com',
      plan: 'pro',
      status: 'active',
      role: 'user',
      is_active: true,
      onboarding_complete: true,
      data_sharing_consent: true,
    },
  ],
};

function summary(overrides: Partial<EvalSummary> = {}): EvalSummary {
  return {
    turns_total: 40,
    turns_completed: 40,
    turns_failed: 0,
    agreement_counts: { identical: 40 },
    safety_counts: {},
    blocking_findings: 0,
    judge_counts: {},
    identical_rate: 1,
    divergence_rate: 0,
    silent_noop_rate: 0,
    baseline: {
      provider: 'anthropic',
      model: 'incumbent',
      input_tokens: 100,
      output_tokens: 10,
      cache_read_tokens: 900,
      cache_creation_tokens: 0,
      cache_read_ratio: 0.9,
      total_cost_usd: '1.500000',
      pricing_available: true,
      latency_p50_ms: 1200,
      latency_p95_ms: 2400,
    },
    candidate: {
      provider: 'anthropic',
      model: 'candidate',
      input_tokens: 1000,
      output_tokens: 10,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      cache_read_ratio: 0,
      total_cost_usd: '0.400000',
      pricing_available: true,
      latency_p50_ms: 600,
      latency_p95_ms: 900,
    },
    recommendation: 'safe_to_switch',
    reasons: ['no safety findings across 40 turns'],
    warnings: [],
    ...overrides,
  };
}

function run(overrides: Partial<EvalRun> = {}): EvalRun {
  return {
    id: 1,
    user_id: 'user-1',
    baseline_provider: 'anthropic',
    baseline_model: 'incumbent',
    candidate_provider: 'anthropic',
    candidate_model: 'candidate',
    judge_model: 'incumbent',
    requested_samples: 40,
    status: 'completed',
    progress_completed: 40,
    progress_total: 40,
    recommendation: 'safe_to_switch',
    error: '',
    created_at: '2026-05-01T12:00:00Z',
    summary: summary(),
    ...overrides,
  };
}

function turn(overrides: Partial<EvalTurn> = {}): EvalTurn {
  return {
    message_seq: 1,
    message_timestamp: '2026-05-01T12:00:00Z',
    user_message: 'can you book that job',
    historic_reply: '',
    historic_tool_names: [],
    baseline: {
      text: '',
      tool_calls: [{ name: 'create_job', arguments: { customer: 'Acme Plumbing' } }],
      stop_reason: 'tool_use',
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      latency_ms: 0,
      error: '',
    },
    candidate: {
      text: 'Sure, I can help with that.',
      tool_calls: [],
      stop_reason: 'end_turn',
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      latency_ms: 0,
      error: '',
    },
    agreement: 'replied_instead_of_acting',
    safety_issues: [],
    judge_verdict: 'not_judged',
    judge_rationale: '',
    ...overrides,
  };
}

function report(overrides: Partial<EvalReport> = {}): EvalReport {
  return { run: run(), turns: [turn()], total_turns: 1, ...overrides };
}


function renderTab() {
  return render(
    <MemoryRouter>
      <ModelEvalTab />
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.getAdminUsers).mockReset().mockResolvedValue(USERS as never);
  vi.mocked(api.listEvalRuns).mockReset().mockResolvedValue([]);
  vi.mocked(api.startEvalRun).mockReset();
  // Defaulted, not bare: starting a run now selects it, so every test that
  // clicks Run analysis immediately fetches a report.
  vi.mocked(api.getEvalReport).mockReset().mockResolvedValue(report());
  vi.mocked(api.cancelEvalRun).mockReset();
});

describe('ModelEvalTab', () => {
  it('only offers users who consented to data sharing', async () => {
    const api = await import('../admin-api');
    renderTab();

    await waitFor(() => expect(api.getAdminUsers).toHaveBeenCalled());
    // A run reads real conversations, so a non-consenting user must not be
    // reachable from the picker at all.
    expect(vi.mocked(api.getAdminUsers)).toHaveBeenCalledWith(
      expect.objectContaining({ consent: 'shared' }),
    );
  });

  it('starts a run with the chosen candidate and sample count', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.startEvalRun).mockResolvedValue(run({ status: 'pending' }));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.type(screen.getByLabelText('provider'), 'anthropic');
    await userEvent.type(screen.getByLabelText('model'), 'candidate');

    await userEvent.click(screen.getByRole('button', { name: 'Run analysis' }));

    await waitFor(() =>
      expect(api.startEvalRun).toHaveBeenCalledWith('user-1', {
        candidateProvider: 'anthropic',
        candidateModel: 'candidate',
        sampleCount: 100,
        judgeEnabled: true,
      }),
    );
  });

  it('renders the verdict and its reasons', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue([run()]);
    vi.mocked(api.getEvalReport).mockResolvedValue(report());
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    const row = await screen.findByText('candidate');
    await userEvent.click(row);

    // The verdict appears twice on purpose: as the pill on the run row and as
    // the banner above the report.
    await waitFor(() => expect(screen.getAllByText('Safe to switch')).toHaveLength(2));
    expect(screen.getByText('no safety findings across 40 turns')).toBeInTheDocument();
    // The switch itself lives on user detail; the report only points at it.
    expect(screen.getByRole('link', { name: 'Model settings for this user' })).toHaveAttribute(
      'href',
      '/app/admin/users/user-1/llm',
    );
  });

  it('surfaces a cost warning rather than reporting an unpriced model as free', async () => {
    const api = await import('../admin-api');
    const withWarning = summary({
      warnings: ['No pricing data for the candidate model (candidate); its cost is reported as zero.'],
      candidate: { ...summary().candidate, pricing_available: false, total_cost_usd: '0.000000' },
    });
    vi.mocked(api.listEvalRuns).mockResolvedValue([run({ summary: withWarning })]);
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({ run: run({ summary: withWarning }) }),
    );
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.click(await screen.findByText('candidate'));

    expect(await screen.findByText(/No pricing data/)).toBeInTheDocument();
    // "$0.0000" would read as free; an unpriced model has to say so.
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('expands a turn with a safety finding by default', async () => {
    const api = await import('../admin-api');
    const flagged = turn({
      safety_issues: [
        { finding: 'unrequested_mutation', tool_name: 'send_message', detail: 'approval-gated' },
      ],
    });
    vi.mocked(api.listEvalRuns).mockResolvedValue([run()]);
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [flagged] }));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.click(await screen.findByText('candidate'));

    // A finding an admin has to click to discover is a finding they will miss.
    const toggle = await screen.findByRole('button', { expanded: true });
    expect(
      within(toggle).getByText(/Reached for a mutating tool the incumbent did not/),
    ).toBeInTheDocument();
    expect(screen.getByText('Incumbent')).toBeInTheDocument();
  });

  it('refetches the report when the selected run finishes', async () => {
    // Regression: the report was fetched only when the selection changed, so a
    // run selected while pending sat on "no summary" for good while the row
    // beside it already read completed.
    const api = await import('../admin-api');
    const running = run({ status: 'running', progress_completed: 3, summary: null });
    vi.mocked(api.listEvalRuns)
      .mockResolvedValueOnce([running])
      .mockResolvedValue([run({ status: 'completed' })]);
    vi.mocked(api.getEvalReport)
      .mockResolvedValueOnce({ run: running, turns: [], total_turns: 0 })
      .mockResolvedValue(report());
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.click(await screen.findByText('candidate'));
    await waitFor(() => expect(api.getEvalReport).toHaveBeenCalledTimes(1));

    // The poll picks up the completed status, which has to re-drive the report.
    await waitFor(() => expect(api.getEvalReport).toHaveBeenCalledTimes(2), { timeout: 6000 });
    await waitFor(() => expect(screen.getAllByText('Safe to switch').length).toBeGreaterThan(0));
  });

  it('pages the turn list and offers to load the rest', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue([run()]);
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({ turns: [turn()], total_turns: 120 }),
    );
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.click(await screen.findByText('candidate'));

    // The count has to be visible, or a partial report reads as the whole run.
    expect(await screen.findByText(/1 of 120/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show more turns' }));
    await waitFor(() => expect(api.getEvalReport).toHaveBeenLastCalledWith(1, 100));
  });

  it('counts only blocking findings in the safety tile', async () => {
    // A provider error is recorded on the turn but must not inflate the tile
    // that says "any finding blocks a switch".
    const api = await import('../admin-api');
    const s = summary({ safety_counts: { call_failed: 3 }, blocking_findings: 0 });
    vi.mocked(api.listEvalRuns).mockResolvedValue([run({ summary: s })]);
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.click(await screen.findByText('candidate'));

    const tile = (await screen.findByText('Safety findings')).closest('div');
    expect(tile).not.toBeNull();
    expect(within(tile as HTMLElement).getByText('0')).toBeInTheDocument();
  });

  it('blocks a second run while one is in flight and offers to cancel', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue([
      run({ status: 'running', progress_completed: 12, progress_total: 40, summary: null }),
    ]);
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    expect(await screen.findByText(/Replaying 12 of 40 turns/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run analysis' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });
});
