import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ModelEvalReportPage from './model-eval-report';
import { report, run, summary, turn } from './model-eval-fixtures';

// ---------------------------------------------------------------------------
// One evaluation run's report, at its own URL.
//
// The report is what a switching decision is actually made on, so these tests
// care most about the ways it could read as more reassuring than the run was:
// an unpriced model showing as free, a provider error inflating the tile that
// says findings block a switch, or a first page of turns passing for the whole
// run.
// ---------------------------------------------------------------------------

vi.mock('../admin-api', () => ({
  getEvalReport: vi.fn(),
  cancelEvalRun: vi.fn(),
}));

function renderReport(runId = 'run-0001') {
  return render(
    <MemoryRouter>
      <ModelEvalReportPage runId={runId} />
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.getEvalReport).mockReset().mockResolvedValue(report());
  vi.mocked(api.cancelEvalRun).mockReset();
});

describe('ModelEvalReportPage', () => {
  it('fetches the run named in the URL', async () => {
    const api = await import('../admin-api');
    renderReport('run-abc');

    await waitFor(() => expect(api.getEvalReport).toHaveBeenCalledWith('run-abc', 50));
  });

  it('renders the verdict and its reasons', async () => {
    renderReport();

    expect(await screen.findByText('Safe to switch')).toBeInTheDocument();
    expect(screen.getByText('no safety findings across 40 turns')).toBeInTheDocument();
    // The switch itself lives on user detail; the report only points at it.
    expect(screen.getByRole('link', { name: 'Model settings for this user' })).toHaveAttribute(
      'href',
      '/app/admin/users/user-1/llm',
    );
  });

  it('offers a way back to the run list', async () => {
    renderReport();

    const back = await screen.findByRole('link', { name: 'Back to model comparison' });
    expect(back).toHaveAttribute('href', '/app/admin/model-eval');
  });

  it('surfaces a cost warning rather than reporting an unpriced model as free', async () => {
    const api = await import('../admin-api');
    const withWarning = summary({
      warnings: ['No pricing data for the candidate model (candidate); its cost is reported as zero.'],
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({ run: run({ summary: withWarning }) }),
    );
    renderReport();

    expect(await screen.findByText(/No pricing data for the candidate model/)).toBeInTheDocument();
  });

  it('counts only blocking findings in the safety tile', async () => {
    // A provider error is recorded on the turn but must not inflate the tile
    // that says "any finding blocks a switch".
    const api = await import('../admin-api');
    const s = summary({ safety_counts: { call_failed: 3 }, blocking_findings: 0 });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    const tile = (await screen.findByText('Blocking findings')).closest('div');
    expect(tile).not.toBeNull();
    expect(within(tile as HTMLElement).getByText('0')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Severity, not just "something was flagged"
  // -------------------------------------------------------------------------

  it('does not dress an advisory finding as an accusation', async () => {
    // ``unresolved_tool_name`` describes the replayed fixture: both models
    // copy a retired tool name out of the history and the summary says so.
    // Rendering it in the same red as a real blocker made the first screen of
    // a report badges that the text below goes on to disown.
    const api = await import('../admin-api');
    const advisory = turn({
      safety_issues: [
        {
          finding: 'unresolved_tool_name',
          tool_name: 'retired_tool',
          detail: 'in the replayed history but not in the current tool schema',
          blocking: false,
        },
      ],
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [advisory] }));
    renderReport();

    const badge = await screen.findByText(/Retired tool name/);
    expect(badge.className).not.toContain('error');
    expect(badge.className).toContain('text-muted-foreground');
  });

  it('renders a blocking finding in the error style', async () => {
    const api = await import('../admin-api');
    const blocking = turn({
      safety_issues: [
        {
          finding: 'unrequested_mutation',
          tool_name: 'qb_update',
          detail: 'nobody asked',
          blocking: true,
        },
      ],
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [blocking] }));
    renderReport();

    const badge = await screen.findByText(/Wrote something neither/);
    expect(badge.className).toContain('error');
  });

  it('explains an unjudged turn instead of leaving it blank', async () => {
    const api = await import('../admin-api');
    const skipped = turn({ judge_verdict: 'not_judged', judge_skip_reason: 'identical' });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [skipped] }));
    renderReport();

    // Scoped to the badge: the run-level accounting line under the summary
    // grid says the same thing about the whole run.
    expect(
      await screen.findByText('Not judged: both models made the same call'),
    ).toBeInTheDocument();
  });

  it('accounts for every turn the judge did not score', async () => {
    const api = await import('../admin-api');
    const s = summary({
      turns_total: 40,
      judge_counts: { equivalent: 26 },
      judge_skip_counts: { identical: 9, blocking_finding: 5 },
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    expect(await screen.findByText(/9 where both models made the same call/)).toBeInTheDocument();
    expect(screen.getByText(/5 already disqualified by a finding/)).toBeInTheDocument();
  });

  it('reports a pre-field run without claiming the judge saw its no-ops', async () => {
    // null means never measured; reading it as a measured zero told the
    // operator the judge had preferred no-ops it never saw.
    const api = await import('../admin-api');
    const s = summary({
      turns_completed: 40,
      silent_noop_rate: 0.05,
      silent_noop_blocking_rate: null,
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    // The same words are the agreement label on a turn card, so scope to the
    // tile: only the summary grid renders the label as a <p>.
    const labels = await screen.findAllByText('Replied instead of acting');
    const tile = labels.find(el => el.tagName === 'P')?.closest('div');
    expect(tile).toBeDefined();
    expect(within(tile as HTMLElement).getByText('5%')).toBeInTheDocument();
    expect(within(tile as HTMLElement).queryByText(/the judge preferred/)).toBeNull();
  });

  it('does not count a silent no-op the judge preferred against the candidate', async () => {
    // Prose is the right answer to some messages, and the incumbent firing a
    // tool at those is the worse decision.
    const api = await import('../admin-api');
    const s = summary({
      turns_completed: 40,
      silent_noop_rate: 0.05,
      silent_noop_blocking_rate: 0,
    });
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    const tile = (await screen.findByText('Replied where acting was better')).closest('div');
    expect(within(tile as HTMLElement).getByText('0%')).toBeInTheDocument();
    expect(within(tile as HTMLElement).getByText(/the judge preferred the rest/)).toBeInTheDocument();
  });

  it('reports cache participation rather than the order-dependent read ratio', async () => {
    // These are the same incumbent on the same prompts hours apart: the read
    // ratio swings on whether an earlier run left warm entries behind.
    const api = await import('../admin-api');
    const s = summary();
    s.baseline = { ...s.baseline, cache_read_ratio: 0.07, cache_participation_ratio: 0.96 };
    s.candidate = { ...s.candidate, cache_read_ratio: 0.01, cache_participation_ratio: 0.01 };
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    const tile = (await screen.findByText('Candidate prompt caching')).closest('div');
    expect(within(tile as HTMLElement).getByText('1%')).toBeInTheDocument();
    expect(within(tile as HTMLElement).getByText(/Incumbent 96%/)).toBeInTheDocument();
  });

  it('shows billed prompt tokens rather than the raw input column', async () => {
    // The raw columns suggest one model was handed 16x the context when both
    // got a byte-identical prompt.
    const api = await import('../admin-api');
    const t = turn({
      safety_issues: [
        {
          finding: 'unrequested_mutation',
          tool_name: 'qb_update',
          detail: 'nobody asked',
          blocking: true,
        },
      ],
    });
    t.baseline = {
      ...t.baseline,
      input_tokens: 9329,
      cache_read_tokens: 18288,
      cache_creation_tokens: 223482,
    };
    t.candidate = {
      ...t.candidate,
      input_tokens: 145270,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
    };
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [t] }));
    renderReport();

    expect(await screen.findByText(/billed prompt tokens/)).toHaveTextContent(
      /incumbent 251,099.*candidate 145,270/,
    );
  });

  it('says a model is unpriced rather than showing a cost of zero', async () => {
    const api = await import('../admin-api');
    const s = summary();
    s.baseline = { ...s.baseline, pricing_available: false, total_cost_usd: '0.000000' };
    s.candidate = { ...s.candidate, pricing_available: false, total_cost_usd: '0.000000' };
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ run: run({ summary: s }) }));
    renderReport();

    const tile = (await screen.findByText('Cost per run')).closest('div');
    expect(within(tile as HTMLElement).getByText('not priced')).toBeInTheDocument();
  });

  it('pages the turn list and offers to load the rest', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getEvalReport).mockResolvedValue(report({ turns: [turn()], total_turns: 120 }));
    renderReport();

    // The count has to be visible, or a partial report reads as the whole run.
    expect(await screen.findByText(/1 of 120/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show more turns' }));
    await waitFor(() => expect(api.getEvalReport).toHaveBeenLastCalledWith('run-0001', 100));
  });

  it('shows progress and the turns already in for a run still going', async () => {
    // The page is worth opening before the run finishes, including from a
    // different browser than the one that started it.
    const api = await import('../admin-api');
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({
        run: run({ status: 'running', progress_completed: 3, progress_total: 40, summary: null }),
        turns: [turn()],
        total_turns: 3,
      }),
    );
    renderReport();

    expect(await screen.findByText(/Replaying 3 of 40 turns/)).toBeInTheDocument();
    expect(screen.getByText(/verdict is written when the run finishes/)).toBeInTheDocument();
    expect(screen.getByText(/Turns, most concerning first/)).toBeInTheDocument();
  });

  it('cancels the run it is showing', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({ run: run({ status: 'running', summary: null }) }),
    );
    vi.mocked(api.cancelEvalRun).mockResolvedValue(run({ status: 'cancelled' }));
    renderReport();

    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(api.cancelEvalRun).toHaveBeenCalledWith('run-0001'));
  });

  it('shows why a run stopped early, beside its partial verdict', async () => {
    // A run the provider killed still carries a summary, so the reason has to
    // be its own line: an "inconclusive" banner alone reads as "we looked and
    // could not tell" rather than "the provider was down".
    const api = await import('../admin-api');
    vi.mocked(api.getEvalReport).mockResolvedValue(
      report({
        run: run({
          status: 'failed',
          error: 'stopped after 3 consecutive provider failures: APIStatusError: 503',
          recommendation: 'inconclusive',
          summary: summary({ recommendation: 'inconclusive', reasons: ['stopped after 3'] }),
        }),
      }),
    );
    renderReport();

    expect(await screen.findByText(/3 consecutive provider failures/)).toBeInTheDocument();
    expect(screen.getByText('Inconclusive')).toBeInTheDocument();
  });

  it('offers a way back when the id in the URL is not a run', async () => {
    // A pasted link with a stale id is the common failure here, and a red
    // banner with no exit is a dead end.
    const api = await import('../admin-api');
    vi.mocked(api.getEvalReport).mockRejectedValue(new Error('Run not found'));
    renderReport('nope');

    expect(await screen.findByText('That evaluation run does not exist.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to model comparison' })).toBeInTheDocument();
  });
});
