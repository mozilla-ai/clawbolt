import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  deleteEvalRun: vi.fn(),
}));

vi.mock('../llm-picker', () => ({
  LLMProviderSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input aria-label="provider" value={value} onChange={e => onChange(e.target.value)} />
  ),
  LLMModelSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input aria-label="model" value={value} onChange={e => onChange(e.target.value)} />
  ),
}));

// The run history renders twice: a card list up to ``xl`` and a table above
// it, swapped by a CSS media query that jsdom does not evaluate. So a query
// naming a run matches once per layout. Tests that are not about the layouts
// themselves go through these; the layouts are checked against each other in
// "shows every run in both layouts".
const listed = (text: string) => screen.queryAllByText(text).length;
const deleteControl = (model: string) => {
  const [control] = screen.getAllByRole('button', { name: `Delete run against ${model}` });
  if (!control) throw new Error(`no delete control for ${model}`);
  return control;
};

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
  vi.mocked(api.deleteEvalRun).mockReset().mockResolvedValue(undefined);
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

  it('lists runs across every user before one is picked', async () => {
    // The table is how a run is found again weeks later, when nobody
    // remembers whose it was.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([
        run(),
        run({
          id: 'run-0002',
          user_id: 'user-2',
          user_email: 'other@example.com',
          candidate_model: 'other-candidate',
        }),
      ]),
    );
    renderTab();

    expect(await screen.findByText('Recent evaluations')).toBeInTheDocument();
    await screen.findAllByText('other@example.com');
    expect(listed('other-candidate')).toBeGreaterThan(0);
    // Unfiltered: the API is asked for every user's runs.
    expect(api.listEvalRuns).toHaveBeenCalledWith(
      expect.objectContaining({ userId: undefined }),
    );
  });

  it('narrows the table to the selected user', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    await waitFor(() =>
      expect(api.listEvalRuns).toHaveBeenCalledWith(expect.objectContaining({ userId: 'user-1' })),
    );
    expect(await screen.findByText('Runs for this user')).toBeInTheDocument();
  });

  it('does not offer a report for a run whose user withdrew consent', async () => {
    // The run row survives, because it is metadata rather than conversation
    // content, but its evidence is no longer readable and the link would 403.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([run({ user_consented: false })]),
    );
    renderTab();

    await screen.findAllByText('consent withdrawn');
    expect(screen.queryByRole('link', { name: /ago|2026/ })).not.toBeInTheDocument();
  });

  it('pages the run table', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()], { total: 60 }));
    renderTab();

    expect(await screen.findByText('1 of 60')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show more runs' }));
    await waitFor(() =>
      expect(api.listEvalRuns).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 50 })),
    );
  });

  it('lets a run start while another user has one in flight', async () => {
    // start_run allows one active run per user, so someone else's run in the
    // unfiltered table must not disable this form.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([
        run({
          id: 'run-0009',
          user_id: 'user-2',
          user_email: 'other@example.com',
          status: 'running',
          summary: null,
        }),
      ]),
    );
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await userEvent.type(screen.getByLabelText('provider'), 'anthropic');
    await userEvent.type(screen.getByLabelText('model'), 'candidate');

    expect(screen.getByRole('button', { name: 'Run analysis' })).not.toBeDisabled();
  });

  it('links each past run to its own report URL', async () => {
    // The report is a page an operator returns to and shares, so the row has
    // to carry a real, copyable link rather than only a click handler.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');

    // One per layout, and both have to point at the run: a card whose
    // timestamp went nowhere would strand every phone visitor on the list.
    const links = await screen.findAllByRole('link', { name: /ago|2026/ });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute('href', '/app/admin/model-eval/run-0001');
    }
  });

  it('does not render a report inline', async () => {
    // Regression: the report used to expand under the form, which meant no
    // URL for it and no way back to one.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findByRole('option', { name: 'consenting@example.com' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'User' }), 'user-1');
    await screen.findAllByText('candidate');

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

  it('shows every run in both layouts', async () => {
    // Two pieces of markup for one row can drift, and the direction it drifts
    // is invisible to whoever changed it: a column added to the table and
    // forgotten on the card costs nothing on a desktop and hides the field on
    // every phone. So whatever the table says about a run, the card says too.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    const table = await screen.findByRole('table');
    const cards = screen.getByRole('list');

    for (const layout of [table, cards]) {
      expect(within(layout).getByText('candidate')).toBeInTheDocument();
      expect(within(layout).getByText(/incumbent/)).toBeInTheDocument();
      expect(within(layout).getByText('consenting@example.com')).toBeInTheDocument();
      expect(within(layout).getByText('Safe to switch')).toBeInTheDocument();
      expect(within(layout).getByText(/completed/)).toBeInTheDocument();
      expect(within(layout).getByText(/\b40\b/)).toBeInTheDocument();
      expect(within(layout).getByRole('link', { name: /ago|2026/ })).toBeInTheDocument();
      expect(
        within(layout).getByRole('button', { name: 'Delete run against candidate' }),
      ).toBeInTheDocument();
    }
  });

  it('keeps the table scroller a containing block', async () => {
    // Not styling. The header's "Actions" label is ``sr-only``, which is
    // absolutely positioned, so without a positioned ancestor its containing
    // block is the viewport rather than this scroller. It then escapes the
    // clip, sits at the far edge of a table wider than the screen, and drags
    // the document out to match, at which point a phone renders the whole
    // page zoomed out to fit. That was the original mobile bug here, and it
    // came back the moment the class was dropped. jsdom has no layout engine,
    // so the class is the only part of this that can be asserted.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    const scroller = (await screen.findByRole('table')).parentElement;
    expect(scroller).toHaveClass('overflow-x-auto');
    expect(scroller).toHaveClass('relative');
  });

  it('will not delete a run until the confirmation is accepted', async () => {
    // The gate is the whole feature: the row itself navigates to the report,
    // so a delete control that fired on the first click would be one stray
    // click away from destroying evidence that costs a replay to rebuild.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findAllByText('candidate');
    await userEvent.click(deleteControl('candidate'));
    expect(api.deleteEvalRun).not.toHaveBeenCalled();

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('cannot be undone');
    await userEvent.click(screen.getByRole('button', { name: 'Delete run' }));

    await waitFor(() => expect(api.deleteEvalRun).toHaveBeenCalledWith('run-0001'));
  });

  it('drops the deleted row without refetching the list', async () => {
    // Refetching would snap a list paged several clicks deep back to page one.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([run(), run({ id: 'run-0002', candidate_model: 'other-candidate' })]),
    );
    renderTab();

    await screen.findAllByText('candidate');
    await userEvent.click(deleteControl('candidate'));
    await userEvent.click(screen.getByRole('button', { name: 'Delete run' }));

    // Gone from both layouts, not just the one the click went through.
    await waitFor(() => expect(listed('candidate')).toBe(0));
    expect(listed('other-candidate')).toBeGreaterThan(0);
    expect(vi.mocked(api.listEvalRuns).mock.calls.length).toBe(1);
  });

  it('cancelling the dialog leaves the run alone', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    renderTab();

    await screen.findAllByText('candidate');
    await userEvent.click(deleteControl('candidate'));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.deleteEvalRun).not.toHaveBeenCalled();
    expect(listed('candidate')).toBeGreaterThan(0);
  });

  it('offers no delete control while a run is still going', async () => {
    // The API refuses it with a 409, so the remedy is Cancel, not Delete.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(
      runList([run({ status: 'running', summary: null })]),
    );
    renderTab();

    await screen.findAllByText('candidate');
    // In neither layout, so a phone cannot reach what the table withholds.
    expect(screen.queryAllByRole('button', { name: 'Delete run against candidate' })).toHaveLength(
      0,
    );
  });

  it('keeps the delete control on a run whose user withdrew consent', async () => {
    // Its report already 403s, so this is the row most worth clearing out and
    // the only control that can still act on it.
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run({ user_consented: false })]));
    renderTab();

    expect(
      await screen.findAllByRole('button', { name: 'Delete run against candidate' }),
    ).toHaveLength(2);
  });

  it('surfaces a failed delete and keeps the row', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listEvalRuns).mockResolvedValue(runList([run()]));
    vi.mocked(api.deleteEvalRun).mockRejectedValue(
      new Error('Run is still running. Cancel it before deleting.'),
    );
    renderTab();

    await screen.findAllByText('candidate');
    await userEvent.click(deleteControl('candidate'));
    await userEvent.click(screen.getByRole('button', { name: 'Delete run' }));

    expect(await screen.findByText(/Cancel it before deleting/)).toBeInTheDocument();
    expect(listed('candidate')).toBeGreaterThan(0);
  });
});
