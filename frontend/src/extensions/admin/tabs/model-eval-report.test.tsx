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

    const tile = (await screen.findByText('Safety findings')).closest('div');
    expect(tile).not.toBeNull();
    expect(within(tile as HTMLElement).getByText('0')).toBeInTheDocument();
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
