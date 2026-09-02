import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ModelEvalTab from './model-eval';
import { report, run, runList, USERS } from './model-eval-fixtures';

// ---------------------------------------------------------------------------
// Admin Model Eval tab: starting runs and listing them.
//
// One run's evidence is a separate page (model-eval-report.test.tsx). What is
// left here is the form, and the tests care most about the ways it could
// mislead: offering a user who cannot legally be evaluated, offering a sample
// size the API will reject, or letting a second run start while one is in
// flight.
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
  vi.mocked(api.listEvalRuns).mockReset().mockResolvedValue(runList([]));
  vi.mocked(api.startEvalRun).mockReset();
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

  it('starts a run with the chosen candidate and the slider size', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.startEvalRun).mockResolvedValue(run({ status: 'pending' }));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.type(screen.getByLabelText('provider'), 'anthropic');
    await userEvent.type(screen.getByLabelText('model'), 'candidate');

    const slider = screen.getByRole('slider', { name: 'Turns to replay' });
    fireEvent.change(slider, { target: { value: '65' } });
    expect(await screen.findByText('Most recent 65')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Run analysis' }));

    await waitFor(() =>
      expect(api.startEvalRun).toHaveBeenCalledWith('user-1', {
        candidateProvider: 'anthropic',
        candidateModel: 'candidate',
        sampleCount: 65,
        judgeEnabled: true,
      }),
    );
  });

  it('bounds the slider by the cap the API reports', async () => {
    // LLM_EVAL_MAX_SAMPLES is configurable, so a slider holding its own
    // ceiling would offer a size start_run rejects with a bare 422.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([], { max_samples: 40 }));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    const slider = await screen.findByRole('slider', { name: 'Turns to replay' });
    await waitFor(() => expect(slider).toHaveAttribute('max', '40'));
    // The default of 100 sits above that cap, so it has to come down with it.
    expect(await screen.findByText('Most recent 40')).toBeInTheDocument();
  });

  it('warns when the chosen size cannot produce a verdict', async () => {
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    const slider = await screen.findByRole('slider', { name: 'Turns to replay' });
    fireEvent.change(slider, { target: { value: '15' } });

    // Better said before the run than after spending one to read
    // "inconclusive" at the end.
    expect(await screen.findByText(/reports inconclusive/)).toBeInTheDocument();
  });

  it('links each past run to its own report URL', async () => {
    // The report is a page an operator returns to and shares, so the row has
    // to carry a real, copyable link rather than only a click handler.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    const link = await screen.findByRole('link', { name: /ago|2026/ });
    expect(link).toHaveAttribute('href', '/app/admin/model-eval/run-0001');
  });

  it('does not render a report inline', async () => {
    // Regression: the report used to expand under the form, which meant no
    // URL for it and no way back to one.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await screen.findByText('candidate');

    expect(api.getEvalReport).not.toHaveBeenCalled();
    expect(screen.queryByText('Turns, most concerning first')).not.toBeInTheDocument();
  });

  it('blocks a second run while one is in flight and links to the one running', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([
        run({ status: 'running', progress_completed: 12, progress_total: 40, summary: null }),
      ]),
    );
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    expect(await screen.findByText(/Replaying 12 of 40 turns/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run analysis' })).toBeDisabled();
    expect(screen.getByRole('link', { name: 'Open report' })).toHaveAttribute(
      'href',
      '/app/admin/model-eval/run-0001',
    );
  });
});
