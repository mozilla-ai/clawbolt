import { render, screen, waitFor } from '@testing-library/react';
import UserDetailView, { type UserDetailSection } from './user-detail';
import type { AdminUser } from '../admin-api';

const mockUser: AdminUser = {
  id: 'uuid-alice',
  user_id: 'google_alice',
  email: 'alice@example.com',
  plan: 'free',
  status: 'active',
  role: 'user',
  is_active: true,
  onboarding_complete: true,
  created_at: '2026-03-20T12:00:00',
  last_login_at: '2026-04-23T14:00:00',
  messages_this_month: 14,
  data_sharing_consent: false,
};

const consentingUser: AdminUser = {
  ...mockUser,
  data_sharing_consent: true,
  data_sharing_consent_at: '2026-04-01T00:00:00Z',
};

const fixtures = vi.hoisted(() => ({
  detail: {
    id: 'uuid-alice',
    user_id: 'google_alice',
    email: 'alice@example.com',
    plan: 'free',
    status: 'active',
    role: 'user',
    is_active: true,
    onboarding_complete: true,
    subscription_created_at: '2026-03-20T12:00:00',
    subscription_updated_at: null,
    timezone: 'America/Los_Angeles',
    preferred_channel: 'imessage',
    heartbeat_opt_in: true,
    heartbeat_frequency: '30m',
    tool_configs: [],
    channel_routes: [],
    permissions: { tools: [], resources: [] },
  },
}));

vi.mock('../admin-api', () => ({
  getUserDetail: vi.fn().mockResolvedValue(fixtures.detail),
  getUserUsage: vi.fn().mockResolvedValue({
    messages: { used: 14, limit: 50000 },
    tokens: { used: 1200, limit: 50000000 },
    period_start: '2026-05-01T00:00:00Z',
    period_cost_usd: '0.12',
    lifetime_cost_usd: '1.50',
  }),
  getLLMUsageLogs: vi.fn().mockResolvedValue({ total: 0, items: [] }),
  getUserLLMOverride: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    llm_provider_override: '',
    llm_model_override: '',
    effective_llm_provider: 'openai',
    effective_llm_model: 'gpt-4',
  }),
  updateUserLLMOverride: vi.fn(),
  activateUser: vi.fn().mockResolvedValue(undefined),
  deactivateUser: vi.fn().mockResolvedValue(undefined),
  deleteUser: vi.fn().mockResolvedValue(undefined),
  resetUserQuota: vi.fn().mockResolvedValue(undefined),
  compactUserContext: vi.fn().mockResolvedValue({
    compacted_message_count: 5,
    new_watermark: 7,
    memory_updated: true,
    event_id: 1,
  }),
  exportUserLLMPayloads: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    exported_at: '2026-05-07T00:00:00Z',
    current_era: {
      payload: { schema_version: 1, model: 'claude-test' },
      captured_at: '2026-05-07T00:00:00Z',
      min_message_seq: 7,
      request_id: 'req-current',
      payload_bytes: 256,
    },
    previous_era: null,
  }),
  // Shared-data endpoints used by the consent-gated sub-tabs. The
  // singular conversation endpoint returns null when the user has
  // no conversation yet (the per-test fixtures override this).
  getSharedDataConversation: vi.fn().mockResolvedValue(null),
  getSharedDataHeartbeatLogs: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    consent_at: null,
    items: [],
    total: 0,
  }),
  getSharedDataCompactionEvents: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    consent_at: null,
    items: [],
    total: 0,
  }),
  getSharedDataMemory: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    consent_at: null,
    memory_text: '',
    history_text: '',
    updated_at: null,
  }),
  getSharedDataProfile: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    consent_at: null,
    soul_text: '',
    user_text: '',
    heartbeat_text: '',
    heartbeat_opt_in: true,
    heartbeat_frequency: '30m',
    heartbeat_max_daily: 5,
  }),
  getSharedDataConversationTurns: vi.fn().mockResolvedValue({
    session_id: 'sess',
    user_id: 'uuid-alice',
    consent_at: null,
    turns: [],
    total: 0,
    last_trim_seq: null,
  }),
  getSharedDataApprovalEvents: vi.fn().mockResolvedValue({
    user_id: 'uuid-alice',
    consent_at: null,
    items: [],
    total: 0,
  }),
}));

const renderDetail = (
  user: AdminUser = mockUser,
  currentUserId?: string,
  section: UserDetailSection = 'activity',
) => {
  const onSectionChange = vi.fn();
  const utils = render(
    <UserDetailView
      user={user}
      currentUserId={currentUserId}
      section={section}
      onSectionChange={onSectionChange}
      onBackToOverview={() => {}}
      onBackToUsers={() => {}}
    />,
  );
  return { ...utils, onSectionChange };
};

describe('UserDetailView', () => {
  it('renders the merged sub-tab bar with Activity as default', async () => {
    renderDetail();

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument();
    });

    // Activity is the new default surface; the structural set is the same
    // for consenting and non-consenting users so the mental model is
    // consistent across the population.
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tab', { name: 'Memory' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'LLM' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Profile' })).toBeInTheDocument();
  });

  it('does not render a separate Conversation sub-tab (folded into Activity)', async () => {
    // #404: the Conversation sub-tab was merged into Activity. Activity
    // rows now show full message bodies + tool calls inline, so a
    // separate transcript view is redundant.
    renderDetail();
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('tab', { name: 'Conversation' })).not.toBeInTheDocument();
  });

  it('does not render the old Heartbeats sub-tab (folded into Activity)', async () => {
    renderDetail();
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('tab', { name: 'Heartbeats' })).not.toBeInTheDocument();
  });

  it('shows the consent-gate panel on Activity when the user has not opted in', async () => {
    renderDetail(mockUser);
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    // Default tab is Activity; non-consenting users see the explainer.
    expect(
      screen.getByText(/requires data sharing consent/i),
    ).toBeInTheDocument();
  });

  it('renders the Activity timeline filter bar for consenting users', async () => {
    renderDetail(consentingUser);
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    // The shared activity timeline ships with a "Types:" filter row.
    await waitFor(() =>
      expect(screen.getByText(/Types:/i)).toBeInTheDocument(),
    );
  });

  it('shows messages-this-month in the identity header', async () => {
    renderDetail();
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    expect(screen.getByText('14')).toBeInTheDocument();
  });

  it('disables destructive actions when viewing your own account', async () => {
    renderDetail(mockUser, 'uuid-alice');
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    const deleteBtn = screen.getByRole('button', { name: /delete user/i });
    expect(deleteBtn).toBeDisabled();
  });

  it('disables Export LLM payloads button when user has not opted in', async () => {
    // The button is consent-gated: non-consenting users see it disabled
    // with a tooltip explaining why. The enabled-for-consenting-user case
    // is exercised indirectly by the existing Activity-timeline tests
    // that render with ``consentingUser``; we don't duplicate that path
    // here because it pulls in shared-data fetches that are mocked in the
    // shared sub-tab tests.
    renderDetail(mockUser);
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    const exportBtn = screen.getByRole('button', { name: /export llm payloads/i });
    expect(exportBtn).toBeDisabled();
  });

  it('does not render Reported as a user-detail sub-tab', async () => {
    // Reported stays a top-level admin tab because the underlying queue
    // is global. Cross-links from Reported into User-detail land in PR6.
    renderDetail();
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('tab', { name: 'Reported' })).not.toBeInTheDocument();
  });

  it('shows an error and Retry when the detail fetch fails', async () => {
    const { getUserDetail } = await import('../admin-api');
    vi.mocked(getUserDetail).mockRejectedValueOnce(new Error('boom'));

    renderDetail();

    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument();
    });
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });

  it('Activity rows show message bodies and tool calls inline (#404)', async () => {
    // The Conversation sub-tab is gone in #404; activity rows now
    // surface the same content (full bodies + tool call list) without
    // a per-row click-to-expand.
    const { getSharedDataConversationTurns } = await import('../admin-api');

    // SharedActivityView defaults its date filter to "today" (local), so
    // a hardcoded fixture date silently drifts out of the window once the
    // wall clock moves past it. Anchor the fixture to today's local date
    // so the row stays inside the default window regardless of when the
    // suite runs.
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const inboundTs = `${today}T11:00:00Z`;
    const outboundTs = `${today}T11:00:30Z`;

    vi.mocked(getSharedDataConversationTurns).mockResolvedValue({
      session_id: 'sess-from-activity',
      user_id: 'uuid-alice',
      consent_at: '2026-04-01T00:00:00Z',
      turns: [
        {
          turn_index: 0,
          user_message: {
            seq: 1,
            direction: 'inbound',
            body: 'when is my next appointment',
            thinking: '',
            timestamp: inboundTs,
          },
          agent_reply: {
            seq: 2,
            direction: 'outbound',
            body: 'Tomorrow at 9am',
            thinking: 'They asked when. cal_lookup will return the next event.',
            timestamp: outboundTs,
          },
          tool_calls: [
            {
              tool_call_id: 'call_1',
              name: 'cal_lookup',
              args: {},
              result: '',
              is_error: false,
              receipt: null,
            },
          ],
          started_at: inboundTs,
          finished_at: outboundTs,
        },
      ],
      total: 1,
      last_trim_seq: null,
    });

    renderDetail(consentingUser);

    // Both message bodies render without any expand interaction.
    await waitFor(() =>
      expect(screen.getByText(/when is my next appointment/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Tomorrow at 9am/)).toBeInTheDocument();
    // Tool calls render inline on the agent-reply row.
    expect(screen.getByText(/cal_lookup/)).toBeInTheDocument();
    // Reasoning is captured for the agent reply (issue #456): the
    // dropdown label is visible, but the body stays collapsed until
    // the admin toggles it. Asserts both halves so a future change
    // that auto-expands or hides the row would be caught.
    const reasoningToggle = screen.getByRole('button', { name: /Reasoning/ });
    expect(reasoningToggle).toBeInTheDocument();
    expect(reasoningToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText(/cal_lookup will return/)).not.toBeInTheDocument();
  });

  it('compact-context trigger button opens a confirm modal with keep-recent and hint inputs', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;

    renderDetail(mockUser);
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /compact context/i }),
      ).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /compact context/i }));

    // The modal opens with both form fields and the destructive
    // confirmation copy. We do NOT exercise the submit path here because
    // jsdom's portal-less ConfirmDialog renders a sibling overlay that
    // is sensitive to async state ordering; the API-call path is
    // covered by the admin-router endpoint tests on the backend side.
    expect(await screen.findByLabelText(/keep recent/i)).toHaveValue(0);
    expect(screen.getByLabelText(/steering hint/i)).toBeInTheDocument();
    expect(
      screen.getByText(/rewrites the user's persistent memory files/i),
    ).toBeInTheDocument();
  });

  // The sub-view is route state now (/app/admin/users/{id}/{section}), so the
  // component renders what it is told and reports clicks back up rather than
  // holding the selection itself.
  it('renders the section named by the prop, not always Activity', async () => {
    renderDetail(mockUser, undefined, 'profile');
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Profile' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute(
      'aria-selected',
      'false',
    );
  });

  it('reports sub-tab clicks through onSectionChange instead of self-navigating', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const { onSectionChange } = renderDetail(mockUser);
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Memory' })).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Memory' }));

    expect(onSectionChange).toHaveBeenCalledWith('memory');
    // Still on Activity: the parent owns the transition.
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });
});
