import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  SharedActivityView,
  SharedMemoryView,
  SharedProfileView,
} from './shared';

// The Shared admin tab and its drilldown wrapper were removed in
// #358 / #404; what remains are the per-user views embedded by
// user-detail.tsx. These tests render those views directly with the
// same fixtures the wrapper used to drive.

const fixtures = vi.hoisted(() => {
  // Snapshot envelope for compaction-event fixtures. Defaults match
  // the "field unchanged by this event" shape from the backend so
  // tests don't have to spell out eight nulls per row.
  const emptySnapshot = () => ({
    text: null,
    truncated: false,
    size_bytes: null,
    head: null,
    tail: null,
    sha256: null,
  });
  return ({
  consentingUser: {
    id: 'uuid-bob',
    user_id: 'google_bob',
    email: 'bob@example.com',
    consent_at: '2026-04-20T00:00:00Z',
    conversation_count: 2,
    last_message_at: '2026-04-25T11:59:30Z',
  },
  turns: {
    session_id: 'sess-001',
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    total: 1,
    last_trim_seq: null,
    turns: [
      {
        turn_index: 0,
        user_message: {
          seq: 1,
          direction: 'inbound',
          body: 'find my pending estimates',
          thinking: '',
          timestamp: '2026-04-25T11:59:00Z',
        },
        agent_reply: {
          seq: 2,
          direction: 'outbound',
          body: 'Found 4 estimates',
          thinking: '',
          timestamp: '2026-04-25T11:59:30Z',
        },
        tool_calls: [
          {
            tool_call_id: 'call_1',
            name: 'qb_query',
            args: { query: 'SELECT * FROM Estimate' },
            result: 'Found 4 rows',
            is_error: false,
            receipt: null,
          },
          {
            tool_call_id: 'call_2',
            name: 'companycam_search_projects',
            args: {},
            result: '',
            is_error: true,
            receipt: null,
          },
        ],
        started_at: '2026-04-25T11:59:00Z',
        finished_at: '2026-04-25T11:59:30Z',
      },
    ],
  },
  profile: {
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    soul_text: 'Always call them by their nickname.',
    user_text: 'Prefers terse replies.',
    heartbeat_text: 'Remind on Fridays.',
    heartbeat_opt_in: true,
    heartbeat_frequency: '2h',
    heartbeat_max_daily: 10,
  },
  heartbeatLogs: {
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    total: 1,
    items: [
      {
        id: 1,
        action_type: 'send',
        channel: 'telegram',
        message_text: 'Reminder: pay invoice by Friday',
        reasoning: 'User asked about pending payments earlier today.',
        tasks: '[{"title":"Pay invoice","due":"Friday"}]',
        created_at: '2026-04-25T12:00:00Z',
      },
    ],
  },
  memory: {
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    memory_text: 'Likes coffee. Owns a contracting business.',
    history_text: 'Compacted 2026-04-15: discussed kitchen remodel.',
    updated_at: '2026-04-25T12:00:00Z',
  },
  compactionEvents: {
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    total: 1,
    items: [
      {
        id: 42,
        triggered_at: '2026-04-25T12:00:00Z',
        duration_ms: 800,
        trimmed_count: 5,
        trimmed_chars: 1500,
        input_tokens: 4000,
        output_tokens: 200,
        min_message_seq: 1,
        max_message_seq: 12,
        status: 'completed',
        memory_updated: true,
        user_profile_updated: false,
        soul_updated: false,
        summary_len: 120,
        memory_text_before: emptySnapshot(),
        memory_text_after: emptySnapshot(),
        history_text_before: emptySnapshot(),
        history_text_after: emptySnapshot(),
        user_text_before: emptySnapshot(),
        user_text_after: emptySnapshot(),
        soul_text_before: emptySnapshot(),
        soul_text_after: emptySnapshot(),
        prompt: emptySnapshot(),
        raw_response: emptySnapshot(),
        parsed_response: emptySnapshot(),
      },
    ],
  },
  emptyCompactionEvents: {
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    total: 0,
    items: [],
  },
});
});

vi.mock('../admin-api', () => ({
  getSharedDataConversationTurns: vi.fn(),
  getSharedDataProfile: vi.fn(),
  getSharedDataHeartbeatLogs: vi.fn(),
  getSharedDataMemory: vi.fn(),
  getSharedDataCompactionEvents: vi.fn(),
  getSharedDataApprovalEvents: vi.fn(),
}));

vi.mock('@/lib/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('html-to-image', () => ({
  toBlob: vi.fn().mockResolvedValue(new Blob(['stub'], { type: 'image/png' })),
}));

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.getSharedDataConversationTurns).mockReset();
  vi.mocked(api.getSharedDataProfile).mockReset();
  vi.mocked(api.getSharedDataHeartbeatLogs).mockReset();
  vi.mocked(api.getSharedDataMemory).mockReset();
  vi.mocked(api.getSharedDataCompactionEvents).mockReset();
  // SharedActivityView fetches approvals as part of its merged stream;
  // every test resolves them as an empty list unless it overrides.
  vi.mocked(api.getSharedDataApprovalEvents).mockResolvedValue({
    user_id: 'uuid-bob',
    consent_at: '2026-04-20T00:00:00Z',
    total: 0,
    items: [],
  });
});

describe('SharedActivityView', () => {
  // The activity feed defaults to today's date window. Pin the clock to
  // the fixture date so the merged stream isn't filtered to empty. Only
  // Date is faked so `waitFor`'s real-timer polling still works.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-04-25T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('merges turns / heartbeats / compactions newest-first', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);

    // The fixture's single turn explodes into one User and one Agent
    // row, plus one Heartbeat and one Compaction = 4 total.
    await waitFor(() => expect(screen.getByText('4 / 4')).toBeInTheDocument());
    // Each type pill shows up both on its row(s) and as a filter chip,
    // so getAllByText is safer than getByText.
    expect(screen.getAllByText('User').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Agent').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Heartbeat').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Compaction').length).toBeGreaterThanOrEqual(2);
  });

  it('toggles the feed between newest-first and oldest-first', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    // Keep only the two conversation rows so the ordering assertion is
    // unambiguous: user message at 11:59:00, agent reply at 11:59:30.
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.compactionEvents,
      items: [],
      total: 0,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('2 / 2')).toBeInTheDocument());

    const olderRow = () => screen.getByText('find my pending estimates');
    const newerRow = () => screen.getByText('Found 4 estimates');
    const follows = (a: HTMLElement, b: HTMLElement): boolean =>
      Boolean(
        a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
      );

    // Default is newest-first: the later agent reply renders above the
    // earlier user message, so the older row follows the newer one.
    const toggle = screen.getByRole('button', { name: /newest first/i });
    expect(follows(newerRow(), olderRow())).toBe(true);

    // Flip to oldest-first: the button label and DOM order both invert.
    await user.click(toggle);
    const flipped = screen.getByRole('button', { name: /oldest first/i });
    expect(flipped).toHaveTextContent('Oldest first');
    expect(follows(olderRow(), newerRow())).toBe(true);
  });

  it('shows message bodies and tool call names inline (no row expand)', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.compactionEvents,
      items: [],
      total: 0,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);

    // Bodies and tool names render without any expand interaction.
    await waitFor(() =>
      expect(screen.getByText('find my pending estimates')).toBeInTheDocument(),
    );
    expect(screen.getByText('Found 4 estimates')).toBeInTheDocument();
    expect(screen.getByText(/qb_query/)).toBeInTheDocument();
    expect(screen.getByText(/companycam_search_projects/)).toBeInTheDocument();
    expect(screen.getByText(/1 error/)).toBeInTheDocument();
  });

  it('clicking a tool call expands its args and result', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.compactionEvents,
      items: [],
      total: 0,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText(/qb_query/)).toBeInTheDocument());

    // Args / result are not visible until the tool call row is clicked.
    expect(screen.queryByText(/SELECT \* FROM Estimate/)).not.toBeInTheDocument();
    await user.click(screen.getByText(/qb_query/).closest('button')!);
    await waitFor(() =>
      expect(screen.getByText(/SELECT \* FROM Estimate/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Found 4 rows/)).toBeInTheDocument();
  });

  it('Types filter narrows the visible rows', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('4 / 4')).toBeInTheDocument());

    // Toggle off the User type chip (aria-pressed button); the result
    // count should drop from 4/4 to 3/4 by hiding the user-message row.
    await user.click(screen.getByRole('button', { name: 'User' }));
    await waitFor(() => expect(screen.getByText('3 / 4')).toBeInTheDocument());
  });

  it('search matches tool call names across token boundaries', async () => {
    // The fixture's agent reply is "Found 4 estimates" and includes a
    // tool call named "companycam_search_projects". A query of
    // "company cam" must match that row even though neither the rendered
    // body nor the tool name contain the literal substring "company
    // cam"; matching relies on tokenization plus the tool name being
    // part of the searchable haystack.
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('4 / 4')).toBeInTheDocument());

    const searchBox = screen.getByPlaceholderText(/Search messages/i);
    await user.type(searchBox, 'company cam');
    // Only the agent-reply row carries the companycam tool call.
    await waitFor(() => expect(screen.getByText('1 / 4')).toBeInTheDocument());
    expect(screen.getByText('Found 4 estimates')).toBeInTheDocument();
  });

  it('search matches tool call argument values', async () => {
    // The qb_query tool call has args { query: 'SELECT * FROM Estimate' }.
    // The token "select" appears only inside the serialized args; it is
    // not in the tool name, the rendered body ("Found 4 estimates"),
    // the user message body, or any other row's payload. So a hit
    // proves the args object is reaching the haystack.
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('4 / 4')).toBeInTheDocument());

    const searchBox = screen.getByPlaceholderText(/Search messages/i);
    await user.type(searchBox, 'select estimate');
    await waitFor(() => expect(screen.getByText('1 / 4')).toBeInTheDocument());
    expect(screen.getByText('Found 4 estimates')).toBeInTheDocument();
  });

  it('Refresh button re-invokes the underlying API calls', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('4 / 4')).toBeInTheDocument());

    // Each stream fired exactly once on initial mount.
    expect(vi.mocked(api.getSharedDataConversationTurns)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.getSharedDataHeartbeatLogs)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.getSharedDataCompactionEvents)).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /refresh activity/i }));

    // After the click, every stream re-fetches: 2 calls each.
    await waitFor(() =>
      expect(vi.mocked(api.getSharedDataConversationTurns)).toHaveBeenCalledTimes(2),
    );
    expect(vi.mocked(api.getSharedDataHeartbeatLogs)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.getSharedDataCompactionEvents)).toHaveBeenCalledTimes(2);
  });

  it('defaults the date window to today and hides older fixture rows', async () => {
    // Override the suite-wide system time so "today" is well after the
    // April 25 fixtures, putting them outside the default window. If the
    // default ever regresses to "no window", every fixture row becomes
    // visible and this assertion flips from 0/0 to 4/4.
    vi.setSystemTime(new Date('2026-05-01T12:00:00Z'));
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue(fixtures.heartbeatLogs);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue(fixtures.compactionEvents);

    render(<SharedActivityView user={fixtures.consentingUser} />);

    await waitFor(() => expect(screen.getByText('0 / 0')).toBeInTheDocument());

    // The backend-filtered streams should be invoked with non-empty
    // bounds, not unbounded. Don't assert the exact date string: the
    // default is local-time, which differs from UTC near midnight on
    // CI runners in non-UTC timezones.
    const heartbeatCall = vi.mocked(api.getSharedDataHeartbeatLogs).mock.calls[0];
    if (!heartbeatCall) throw new Error('expected heartbeat logs fetch');
    expect(heartbeatCall[1]?.start_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(heartbeatCall[1]?.end_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('collapses a paired approval request + decision into one row (#507)', async () => {
    // Two adjacent approval events for the same tool used to render as
    // two separate cards, doubling the vertical space the approval flow
    // takes in the feed. They should now merge into a single row whose
    // summary carries the decision and whose body shows both timestamps.
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue({
      ...fixtures.turns,
      turns: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });
    vi.mocked(api.getSharedDataApprovalEvents).mockResolvedValue({
      user_id: 'uuid-bob',
      consent_at: '2026-04-20T00:00:00Z',
      total: 2,
      items: [
        {
          id: 1,
          event_type: 'requested',
          tool_name: 'qb_send_invoice',
          description: 'Send invoice 1234 to customer',
          channel: 'telegram',
          chat_id: 'chat-bob',
          decision: null,
          created_at: '2026-04-25T11:55:00Z',
        },
        {
          id: 2,
          event_type: 'decided',
          tool_name: 'qb_send_invoice',
          description: 'Send invoice 1234 to customer',
          channel: '',
          chat_id: '',
          decision: 'approved',
          created_at: '2026-04-25T11:55:30Z',
        },
      ],
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);

    // Exactly one Approval row, not two. The merged body shows the
    // request's description plus the resolution's decision, so the
    // admin sees the full lifecycle without scanning two cards.
    await waitFor(() => expect(screen.getByText('1 / 1')).toBeInTheDocument());
    expect(screen.getByText(/Send invoice 1234/)).toBeInTheDocument();
    // Labels render with a trailing colon (ActivityField), so match
    // the label text with a regex.
    expect(screen.getByText(/^Requested:/)).toBeInTheDocument();
    expect(screen.getByText(/^Decision:/)).toBeInTheDocument();
    expect(screen.getByText(/approved/)).toBeInTheDocument();
    // The "Approval" pill labels both the filter chip and any rendered
    // rows. Two events used to render two rows (3 total occurrences);
    // merging means we see exactly 2 occurrences: filter chip + 1 row.
    expect(screen.getAllByText('Approval')).toHaveLength(2);
  });

  it('leaves a still-pending approval request as a standalone row', async () => {
    // An approval that has no matching resolution (the user has not
    // responded yet, or the request is on a different tool than the
    // following event) keeps its own row instead of being swallowed.
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue({
      ...fixtures.turns,
      turns: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });
    vi.mocked(api.getSharedDataApprovalEvents).mockResolvedValue({
      user_id: 'uuid-bob',
      consent_at: '2026-04-20T00:00:00Z',
      total: 1,
      items: [
        {
          id: 7,
          event_type: 'requested',
          tool_name: 'qb_send_invoice',
          description: 'Send invoice 9999 to customer',
          channel: 'telegram',
          chat_id: 'chat-bob',
          decision: null,
          created_at: '2026-04-25T11:55:00Z',
        },
      ],
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);

    await waitFor(() => expect(screen.getByText('1 / 1')).toBeInTheDocument());
    // The pending request renders its own row with a "pending" status
    // and the request's description visible; no Decision field appears.
    expect(screen.getByText(/Send invoice 9999/)).toBeInTheDocument();
    expect(screen.getByText('pending')).toBeInTheDocument();
    expect(screen.queryByText(/^Decision:/)).not.toBeInTheDocument();
  });

  it('highlighting rows opens a share snippet dialog with the selected messages', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByText('find my pending estimates')).toBeInTheDocument(),
    );

    // No share affordance until at least one row is highlighted.
    expect(screen.queryByRole('button', { name: /share snippet/i })).not.toBeInTheDocument();

    // Highlight both conversation rows via their checkboxes.
    const checkboxes = screen.getAllByRole('checkbox', { name: /highlight for sharing/i });
    expect(checkboxes).toHaveLength(2);
    await user.click(checkboxes[0]!);
    await user.click(checkboxes[1]!);

    // The selection bar reflects the count and exposes the Share action.
    expect(screen.getByText(/2 highlighted/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /share snippet/i }));

    // The dialog renders the highlighted messages in a clean transcript.
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('find my pending estimates')).toBeInTheDocument();
    expect(within(dialog).getByText('Found 4 estimates')).toBeInTheDocument();
    expect(within(dialog).getByText('User')).toBeInTheDocument();
    expect(within(dialog).getByText('Clawbolt')).toBeInTheDocument();
    // The agent reply's tool calls surface with success/failure marks and a
    // failed-count callout so the snippet conveys what the agent did.
    expect(within(dialog).getByText(/2 tool calls/)).toBeInTheDocument();
    expect(within(dialog).getByText(/1 failed/)).toBeInTheDocument();
    expect(within(dialog).getByText(/✓ qb_query/)).toBeInTheDocument();
    expect(within(dialog).getByText(/✗ companycam_search_projects/)).toBeInTheDocument();
  });

  it('Copy as text writes the highlighted transcript to the clipboard', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByText('find my pending estimates')).toBeInTheDocument(),
    );

    const checkboxes = screen.getAllByRole('checkbox', { name: /highlight for sharing/i });
    await user.click(checkboxes[0]!);
    await user.click(checkboxes[1]!);
    await user.click(screen.getByRole('button', { name: /share snippet/i }));

    // Install the clipboard spy after userEvent.setup(), which otherwise
    // overwrites navigator.clipboard with its own stub.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    await user.click(await screen.findByRole('button', { name: /copy as text/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    // Oldest-first transcript, wrapped in a ``` code fence for Slack. The
    // agent reply lists its tool calls with ✓/✗ marks and a failed-count
    // header. Nothing is expanded in the feed, so no args/result lines appear.
    const copied = writeText.mock.calls[0]![0] as string;
    expect(copied).toBe(
      '```\n' +
        'User: find my pending estimates\n\n' +
        'Clawbolt: Found 4 estimates\n' +
        '  Tools (2, 1 failed):\n' +
        '    ✓ qb_query\n' +
        '    ✗ companycam_search_projects\n' +
        '```',
    );
  });

  it('mirrors an expanded tool call: its result appears in the shared snippet', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);

    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText(/qb_query/)).toBeInTheDocument());

    // The qb_query result is hidden until the admin expands it in the feed.
    expect(screen.queryByText(/Found 4 rows/)).not.toBeInTheDocument();
    await user.click(screen.getByText(/qb_query/).closest('button')!);
    await waitFor(() => expect(screen.getByText(/Found 4 rows/)).toBeInTheDocument());

    // Highlight the agent reply (located by its body, since the feed is
    // newest-first so row order is not the fixture order) and open the dialog.
    const agentRow = screen.getByText('Found 4 estimates').closest('li')!;
    await user.click(
      within(agentRow).getByRole('checkbox', { name: /highlight for sharing/i }),
    );
    await user.click(screen.getByRole('button', { name: /share snippet/i }));
    const dialog = await screen.findByRole('dialog');

    // Because qb_query is expanded in the feed, its args and result are
    // mirrored into the snippet panel; companycam (still collapsed) shows
    // only its name.
    expect(within(dialog).getByText(/Found 4 rows/)).toBeInTheDocument();
    expect(within(dialog).getByText(/SELECT \* FROM Estimate/)).toBeInTheDocument();

    // ... and into the copied text, under a `result:` label.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    await user.click(within(dialog).getByRole('button', { name: /copy as text/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0]![0] as string;
    expect(copied).toContain('result: Found 4 rows');
    expect(copied).toContain('args: {"query":"SELECT * FROM Estimate"}');
  });

  it('mirrors expanded reasoning into the shared snippet', async () => {
    const reasoningText = 'Checked the estimates table before replying.';
    const baseTurn = fixtures.turns.turns[0]!;
    const turnsWithThinking = {
      ...fixtures.turns,
      turns: [
        {
          ...baseTurn,
          agent_reply: { ...baseTurn.agent_reply!, thinking: reasoningText },
          tool_calls: [],
        },
      ],
    };

    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(turnsWithThinking);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('Found 4 estimates')).toBeInTheDocument());

    // Reasoning is collapsed by default, so its text is hidden everywhere.
    expect(screen.queryByText(reasoningText)).not.toBeInTheDocument();

    // Highlight the agent reply and share without expanding reasoning: the
    // snippet must not leak the hidden reasoning text.
    const agentRow = screen.getByText('Found 4 estimates').closest('li')!;
    await user.click(
      within(agentRow).getByRole('checkbox', { name: /highlight for sharing/i }),
    );
    await user.click(screen.getByRole('button', { name: /share snippet/i }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByText(reasoningText)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: /close/i }));

    // Expand reasoning in the feed, then re-open: now it is mirrored.
    await user.click(screen.getByRole('button', { name: /reasoning/i }));
    await waitFor(() => expect(screen.getByText(reasoningText)).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /share snippet/i }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(reasoningText)).toBeInTheDocument();
  });

  it('Download image renders the snippet panel to a PNG and saves it', async () => {
    const htmlToImage = await import('html-to-image');
    vi.mocked(htmlToImage.toBlob).mockClear();

    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataConversationTurns).mockResolvedValue(fixtures.turns);
    vi.mocked(api.getSharedDataHeartbeatLogs).mockResolvedValue({
      ...fixtures.heartbeatLogs,
      items: [],
      total: 0,
    });
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValue({
      ...fixtures.emptyCompactionEvents,
    });

    render(<SharedActivityView user={fixtures.consentingUser} />);
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByText('find my pending estimates')).toBeInTheDocument(),
    );

    const checkboxes = screen.getAllByRole('checkbox', { name: /highlight for sharing/i });
    await user.click(checkboxes[0]!);
    await user.click(screen.getByRole('button', { name: /share snippet/i }));
    const dialog = await screen.findByRole('dialog');

    // Clicking Download image rasterizes the transcript node and triggers a
    // download via a generated <a download="clawbolt-snippet.png">. The anchor
    // must be attached to the document at click time: Firefox for Android
    // ignores a programmatic click on a detached <a>, so a detached link would
    // silently no-op on mobile.
    let attachedAtClick = false;
    let downloadAttr: string | null = null;
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        attachedAtClick = this.isConnected;
        downloadAttr = this.getAttribute('download');
      });
    await user.click(within(dialog).getByRole('button', { name: /download image/i }));
    await waitFor(() => expect(vi.mocked(htmlToImage.toBlob)).toHaveBeenCalledTimes(1));
    expect(click).toHaveBeenCalled();
    expect(attachedAtClick).toBe(true);
    expect(downloadAttr).toBe('clawbolt-snippet.png');
    click.mockRestore();
  });
});

describe('SharedProfileView', () => {
  it('loads soul / user / heartbeat text + heartbeat config', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataProfile).mockResolvedValueOnce(fixtures.profile);

    render(<SharedProfileView user={fixtures.consentingUser} />);

    await waitFor(() =>
      expect(screen.getByText(/Always call them by their nickname/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Prefers terse replies/)).toBeInTheDocument();
    expect(screen.getByText(/Remind on Fridays/)).toBeInTheDocument();
    expect(screen.getByText('2h')).toBeInTheDocument();
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(vi.mocked(api.getSharedDataProfile)).toHaveBeenCalledWith('uuid-bob');
  });
});

describe('SharedMemoryView', () => {
  it('shows memory_text + history_text', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataMemory).mockResolvedValueOnce(fixtures.memory);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValueOnce(
      fixtures.emptyCompactionEvents,
    );

    render(<SharedMemoryView user={fixtures.consentingUser} />);

    await waitFor(() =>
      expect(screen.getByText(/Likes coffee/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/discussed kitchen remodel/)).toBeInTheDocument();
    expect(vi.mocked(api.getSharedDataMemory)).toHaveBeenCalledWith('uuid-bob');
  });

  it('renders compaction events under the memory text', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getSharedDataMemory).mockResolvedValueOnce(fixtures.memory);
    vi.mocked(api.getSharedDataCompactionEvents).mockResolvedValueOnce(
      fixtures.compactionEvents,
    );

    render(<SharedMemoryView user={fixtures.consentingUser} />);

    // The section heading renders before its nested async fetch resolves, so
    // wait for the row content rather than the static shell.
    const rowSummary = await screen.findByText(/5 msg \/ 1,500 chars/);
    // Counts and timing render in the row.
    expect(screen.getByText(/800ms/)).toBeInTheDocument();
    // The row mentions which fields were updated. Match within the
    // row's text content; "memory" alone collides with other UI.
    const row = rowSummary.closest('li');
    expect(row).toBeTruthy();
    expect(row?.textContent).toMatch(/updated.*memory/);
    expect(vi.mocked(api.getSharedDataCompactionEvents)).toHaveBeenCalledWith(
      'uuid-bob',
      expect.any(Object),
    );
  });
});
