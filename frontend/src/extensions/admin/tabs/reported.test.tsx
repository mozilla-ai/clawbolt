import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ReportedTab from './reported';

// ---------------------------------------------------------------------------
// /admin/reported-conversations frontend tests.
//
// Covers the list view (status filter, empty state, click-to-detail) and
// the detail view (anchor highlighting, Dismiss flow). The dismiss
// confirmation goes through ConfirmDialog which uses a vitest-friendly
// modal.
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  empty: { total: 0, open_count: 0, items: [] },
  oneOpen: {
    total: 1,
    open_count: 1,
    items: [
      {
        id: 42,
        user_id: 'uuid-alice',
        user_email: 'alice@example.com',
        session_id: 'sess-abc',
        channel: 'imessage',
        anchor_seq: 5,
        reason: 'bot was rude',
        status: 'open' as const,
        created_at: '2026-04-25T12:00:00Z',
        dismissed_at: null,
        reviewed_admin_email: null,
      },
    ],
  },
  messagesAroundAnchor: {
    report_id: 42,
    session_id: 'sess-abc',
    user_id: 'uuid-alice',
    anchor_seq: 5,
    items: [
      { seq: 4, direction: 'inbound', body: 'before', timestamp: null, is_anchor: false },
      { seq: 5, direction: 'inbound', body: 'the anchor', timestamp: null, is_anchor: true },
      { seq: 6, direction: 'outbound', body: 'after', timestamp: null, is_anchor: false },
    ],
  },
  dismissResp: {
    id: 42,
    dismissed_at: '2026-05-01T15:00:00Z',
    reviewed_admin_user_id: 'uuid-admin',
  },
}));

vi.mock('../admin-api', () => ({
  getReportedConversations: vi.fn(),
  getReportedConversationMessages: vi.fn(),
  dismissReportedConversation: vi.fn(),
}));

vi.mock('@/lib/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.getReportedConversations).mockReset();
  vi.mocked(api.getReportedConversationMessages).mockReset();
  vi.mocked(api.dismissReportedConversation).mockReset();
});

describe('ReportedTab list', () => {
  it('renders empty state when no reports exist', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getReportedConversations).mockResolvedValueOnce(fixtures.empty);

    render(<ReportedTab />);

    await waitFor(() =>
      expect(screen.getByText(/No reports in this view/)).toBeInTheDocument(),
    );
  });

  it('renders one report with status pill and reason', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getReportedConversations).mockResolvedValueOnce(fixtures.oneOpen);

    render(<ReportedTab />);

    await waitFor(() =>
      expect(screen.getByText('alice@example.com')).toBeInTheDocument(),
    );
    expect(screen.getByText('bot was rude')).toBeInTheDocument();
    expect(screen.getByText('open')).toBeInTheDocument();
    expect(screen.getByText(/via imessage/)).toBeInTheDocument();
  });

  it('switches the API status filter when the user picks "Open"', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getReportedConversations).mockResolvedValue(fixtures.oneOpen);

    render(<ReportedTab />);

    await waitFor(() =>
      expect(screen.getByText('alice@example.com')).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Open' }));

    await waitFor(() => {
      const calls = vi.mocked(api.getReportedConversations).mock.calls;
      expect(calls.some(c => c[0]?.status === 'open')).toBe(true);
    });
  });
});

describe('ReportedTab detail + dismiss', () => {
  it('opens detail on click, renders the anchor with a marker', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getReportedConversations).mockResolvedValueOnce(fixtures.oneOpen);
    vi.mocked(api.getReportedConversationMessages).mockResolvedValueOnce(
      fixtures.messagesAroundAnchor,
    );

    render(<ReportedTab />);
    await waitFor(() =>
      expect(screen.getByText('alice@example.com')).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    // Each row is a button. Click the one for alice.
    await user.click(screen.getByText('bot was rude').closest('button')!);

    await waitFor(() =>
      expect(screen.getByText('the anchor')).toBeInTheDocument(),
    );
    // The anchor row gets the (anchor) caption.
    expect(screen.getByText('(anchor)')).toBeInTheDocument();
    // Surrounding messages render too.
    expect(screen.getByText('before')).toBeInTheDocument();
    expect(screen.getByText('after')).toBeInTheDocument();
  });

  it('dismiss button on an open report calls the dismiss API and reloads', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getReportedConversations).mockResolvedValueOnce(fixtures.oneOpen);
    vi.mocked(api.getReportedConversationMessages).mockResolvedValueOnce(
      fixtures.messagesAroundAnchor,
    );
    vi.mocked(api.dismissReportedConversation).mockResolvedValueOnce(fixtures.dismissResp);
    // Second list load (after dismiss) returns empty queue.
    vi.mocked(api.getReportedConversations).mockResolvedValueOnce(fixtures.empty);

    render(<ReportedTab />);
    await waitFor(() =>
      expect(screen.getByText('alice@example.com')).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.click(screen.getByText('bot was rude').closest('button')!);
    await waitFor(() =>
      expect(screen.getByText('the anchor')).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    // ConfirmDialog asks for confirmation. There are now two buttons
    // named "Dismiss" on screen — the one in the detail toolbar and the
    // one inside the dialog. Scope to the dialog to avoid the ambiguity.
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Dismiss' });
    await user.click(confirm);

    await waitFor(() =>
      expect(vi.mocked(api.dismissReportedConversation)).toHaveBeenCalledWith(42),
    );
    // After dismiss we navigate back; the empty queue renders.
    await waitFor(() =>
      expect(screen.getByText(/No reports in this view/)).toBeInTheDocument(),
    );
  });
});
