import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import AdminPanel from './index';

// ── Stub useAuth so AdminPanel has a current user id ──────────────────────

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    authConfig: { method: 'oauth_google', required: true },
    currentAuthUser: { id: 'uuid-admin', name: 'admin@example.com', role: 'admin' },
    isPremium: true,
    handleLogin: () => {},
    handleLogout: () => {},
  }),
}));

// ── Mock admin API ────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => {
  const MOCK_STATS = {
    telegram_configured: true,
    bluebubbles_configured: false,
    twilio_configured: false,
  };

  const MOCK_USERS = {
    total: 2,
    offset: 0,
    limit: 50,
    items: [
      {
        id: 'uuid-1',
        user_id: 'google_alice',
        email: 'alice@example.com',
        plan: 'pro',
        status: 'active',
        role: 'admin',
        is_active: true,
        onboarding_complete: true,
        created_at: '2026-03-20T12:00:00Z',
        last_login_at: '2026-04-23T12:00:00Z',
        last_message_at: '2026-04-28T09:00:00Z',
        messages_this_month: 42,
        data_sharing_consent: true,
        data_sharing_consent_at: '2026-04-01T00:00:00Z',
        conversation_count: 4,
      },
      {
        id: 'uuid-bob',
        user_id: 'google_bob',
        email: 'bob@example.com',
        plan: 'free',
        status: 'none',
        role: 'user',
        is_active: false,
        onboarding_complete: false,
        created_at: '2026-04-01T12:00:00Z',
        last_login_at: null,
        last_message_at: null,
        messages_this_month: 0,
        data_sharing_consent: false,
        data_sharing_consent_at: null,
        conversation_count: 0,
      },
    ],
  };

  const MOCK_ALLOWED_EMAILS = {
    total: 2,
    items: [
      { id: 1, email: 'carol@example.com', note: 'Team lead', created_at: '2026-03-20T00:00:00' },
      { id: 2, email: 'dave@example.com', note: '', created_at: '2026-03-20T00:00:00' },
    ],
  };

  const MOCK_WAITLIST = {
    total: 5,
    items: [
      { id: 1, email: 'eve@example.com', source: 'homepage', created_at: '2026-04-22T00:00:00' },
    ],
  };

  const MOCK_CHANNEL_CONFIG = {
    bluebubbles_server_url: '',
    bluebubbles_password_set: false,
    bluebubbles_imessage_address: '',
    bluebubbles_send_method: '',
    bluebubbles_configured: false,
    telegram_bot_token_set: true,
    telegram_allowed_chat_id: '',
    linq_api_token_set: false,
    linq_from_number: '',
    linq_allowed_numbers: '',
    linq_preferred_service: '',
  };

  const MOCK_MONITORING = {
    alerts: {
      enabled: true,
      pending_groups: 0,
      dedupe_minutes: 30,
      flush_interval_seconds: 60,
      max_emails_per_hour: 10,
    },
    health_monitor: {
      enabled: true,
      interval_seconds: 300,
      failure_threshold: 2,
      probes: {
        database: {
          label: 'Database',
          status: 'up',
          detail: '',
          consecutive_failures: 0,
          since: '2026-05-01T00:00:00Z',
          last_checked: '2026-05-01T00:00:00Z',
          never_connected: false,
        },
      },
      last_run_at: '2026-05-01T00:00:00Z',
      history: [],
      run: null,
    },
    email: {
      configured: true,
      host: 'smtp.example.com',
      port: 587,
      timeout_seconds: 10,
      last_attempt_at: null,
      last_success_at: null,
      last_error: '',
      last_error_at: null,
    },
    recipient_configured: true,
    timestamp: '2026-05-01T00:00:00Z',
  };

  return {
    MOCK_STATS,
    MOCK_USERS,
    MOCK_ALLOWED_EMAILS,
    MOCK_WAITLIST,
    MOCK_CHANNEL_CONFIG,
    MOCK_MONITORING,
  };
});

vi.mock('./admin-api', () => ({
  getAdminStats: vi.fn().mockResolvedValue(mocks.MOCK_STATS),
  getAdminVersion: vi.fn().mockResolvedValue({
    premium_version: '0.7.32',
    premium_commit: 'abc1234',
    oss_version: 'v0.7.32',
    oss_commit: 'def5678',
    started_at: '2026-05-29T00:00:00Z',
  }),
  getAdminUsers: vi.fn().mockResolvedValue(mocks.MOCK_USERS),
  findAdminUserById: vi
    .fn()
    .mockImplementation((id: string) =>
      Promise.resolve(mocks.MOCK_USERS.items.find(u => u.id === id) ?? null),
    ),
  getUserDetail: vi.fn().mockResolvedValue({
    id: 'uuid-1',
    user_id: 'google_alice',
    email: 'alice@example.com',
    plan: 'pro',
    status: 'active',
    role: 'admin',
    is_active: true,
    onboarding_complete: true,
    subscription_created_at: '2026-03-20T00:00:00',
    subscription_updated_at: null,
    timezone: '',
    preferred_channel: '',
    heartbeat_opt_in: true,
    heartbeat_frequency: '30m',
    tool_configs: [],
    channel_routes: [],
    permissions: { tools: [], resources: [] },
  }),
  activateUser: vi.fn().mockResolvedValue(undefined),
  deactivateUser: vi.fn().mockResolvedValue(undefined),
  deleteUser: vi.fn().mockResolvedValue(undefined),
  resetUserQuota: vi.fn().mockResolvedValue(undefined),
  compactUserContext: vi.fn().mockResolvedValue({}),
  exportUserLLMPayloads: vi.fn().mockResolvedValue({}),
  updateUserPlan: vi.fn().mockResolvedValue({}),
  getUserUsage: vi.fn().mockResolvedValue({
    messages: { used: 1, limit: 100 },
    tokens: { used: 10, limit: 1000 },
    period_start: null,
    period_cost_usd: '0.00',
    lifetime_cost_usd: '0.00',
  }),
  getUserLLMOverride: vi.fn().mockResolvedValue({
    user_id: 'uuid-1',
    llm_provider_override: '',
    llm_model_override: '',
    effective_llm_provider: 'openai',
    effective_llm_model: 'gpt-4',
  }),
  updateUserLLMOverride: vi.fn().mockResolvedValue({}),
  getLLMUsageLogs: vi.fn().mockResolvedValue({ total: 0, items: [] }),
  listAllowedEmails: vi.fn().mockResolvedValue(mocks.MOCK_ALLOWED_EMAILS),
  addAllowedEmail: vi.fn().mockResolvedValue(mocks.MOCK_ALLOWED_EMAILS.items[0]),
  removeAllowedEmail: vi.fn().mockResolvedValue(undefined),
  listWaitlistEntries: vi.fn().mockResolvedValue(mocks.MOCK_WAITLIST),
  approveWaitlistEntry: vi.fn().mockResolvedValue(mocks.MOCK_ALLOWED_EMAILS.items[0]),
  dismissWaitlistEntry: vi.fn().mockResolvedValue(undefined),
  getAdminChannelConfig: vi.fn().mockResolvedValue(mocks.MOCK_CHANNEL_CONFIG),
  updateAdminChannelConfig: vi.fn().mockResolvedValue(mocks.MOCK_CHANNEL_CONFIG),
  getAdminLLMConfig: vi.fn().mockResolvedValue({
    llm_provider: 'openai',
    llm_model: 'gpt-4',
    llm_api_base: null,
  }),
  updateAdminLLMConfig: vi.fn().mockResolvedValue({
    llm_provider: 'openai',
    llm_model: 'gpt-4',
    llm_api_base: null,
  }),
  listProviders: vi.fn().mockResolvedValue([{ name: 'openai', local: false }]),
  listProviderModels: vi.fn().mockResolvedValue({
    provider: 'openai',
    models: ['gpt-4'],
    supports_listing: true,
    error: null,
  }),
  invalidateProviderModels: vi.fn(),
  getHeartbeatLogs: vi.fn().mockResolvedValue({ total: 0, items: [] }),
  getReportedConversations: vi
    .fn()
    .mockResolvedValue({ total: 0, open_count: 0, items: [] }),
  getReportedConversationMessages: vi.fn().mockResolvedValue({ items: [] }),
  dismissReportedConversation: vi.fn().mockResolvedValue({}),
  listAdminApiKeys: vi.fn().mockResolvedValue({ items: [] }),
  createAdminApiKey: vi.fn().mockResolvedValue({}),
  revokeAdminApiKey: vi.fn().mockResolvedValue(undefined),
  getMonitoringStatus: vi.fn().mockResolvedValue(mocks.MOCK_MONITORING),
  runHealthProbes: vi.fn().mockResolvedValue({ started: false, detail: '', run: null }),
  sendMonitoringTestAlert: vi.fn().mockResolvedValue({}),
  diagnoseEmailDelivery: vi.fn().mockResolvedValue({}),
  // Pilot panel data. Default to "no consenting users yet" so tests that
  // don't care about the panel still get the explainer empty state.
  getSharedDataSummary: vi.fn().mockResolvedValue({
    consenting_user_count: 0,
    consents_changed_this_week: 0,
    conversations_this_week: 0,
    heartbeats_this_week: 0,
    open_reports_count: 0,
    top_users_this_week: [],
  }),
  getSharedDataUsers: vi.fn().mockResolvedValue({ total: 0, items: [] }),
  getSharedDataConversationTurns: vi.fn().mockResolvedValue(null),
  getSharedDataApprovalEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  getSharedDataHeartbeatLogs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  getSharedDataCompactionEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  getSharedDataMemory: vi.fn().mockResolvedValue({
    user_id: 'uuid-1',
    consent_at: null,
    memory_text: '',
    history_text: '',
    updated_at: null,
  }),
  getSharedDataProfile: vi.fn().mockResolvedValue({
    user_id: 'uuid-1',
    consent_at: null,
    soul_text: '',
    user_text: '',
    heartbeat_text: '',
    heartbeat_opt_in: false,
    heartbeat_frequency: '',
    heartbeat_max_daily: 0,
  }),
}));

// Mounted the way the app mounts it: OSS routes /app/admin/* to the premium
// admin element, and AdminPanel owns everything below that.
let lastLocation = '';

function LocationProbe() {
  lastLocation = useLocation().pathname;
  return null;
}

function renderAdmin(route = '/app/admin') {
  lastLocation = '';
  return render(
    <MemoryRouter initialEntries={[route]}>
      <LocationProbe />
      <Routes>
        <Route path="/app/admin/*" element={<AdminPanel />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminPanel routing', () => {
  it('redirects the admin index to the Overview section', async () => {
    renderAdmin('/app/admin');
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Overview', level: 2 })).toBeInTheDocument();
    });
    expect(lastLocation).toBe('/app/admin/overview');
  });

  it('renders each section at its own URL', async () => {
    renderAdmin('/app/admin/config');
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Config', level: 2 })).toBeInTheDocument();
    });
    expect(
      await screen.findByRole('heading', { level: 4, name: 'Channels' }),
    ).toBeInTheDocument();
  });

  it('renders the Access section directly from its URL', async () => {
    renderAdmin('/app/admin/access');
    await waitFor(() => {
      expect(screen.getByText('carol@example.com')).toBeInTheDocument();
    });
    expect(screen.queryByText(/REGISTRATION_MODE/)).not.toBeInTheDocument();
  });

  it('no longer renders a horizontal tab bar (sections live in the sidebar)', async () => {
    renderAdmin('/app/admin/overview');
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'System and integration health' }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  });

  it('shows a recovery affordance for an unknown admin path', async () => {
    renderAdmin('/app/admin/not-a-section');
    await waitFor(() => {
      expect(screen.getByText(/does not exist/i)).toBeInTheDocument();
    });
    await userEvent.setup().click(screen.getByRole('button', { name: /Go to Overview/i }));
    expect(lastLocation).toBe('/app/admin/overview');
  });

  it('opens user detail from a users/:id URL', async () => {
    renderAdmin('/app/admin/users/uuid-1');
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument();
    });
    expect(screen.getAllByText('alice@example.com').length).toBeGreaterThan(0);
  });

  it('honors the sub-view segment on a user detail URL', async () => {
    renderAdmin('/app/admin/users/uuid-1/profile');
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Profile' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
  });

  it('explains a user id that no longer resolves', async () => {
    renderAdmin('/app/admin/users/uuid-gone');
    await waitFor(() => {
      expect(screen.getByText(/no longer exists/i)).toBeInTheDocument();
    });
  });
});

describe('AdminPanel legacy hash deep links', () => {
  // Pre-#662 the admin sections were hash fragments. The redirect is resolved
  // from the router's own location, so the entry carries the hash.
  it('translates #users into the Users route', async () => {
    renderAdmin('/app/admin#users');
    await waitFor(() => expect(lastLocation).toBe('/app/admin/users'));
  });

  it('translates #users/{id} into the user detail route', async () => {
    renderAdmin('/app/admin#users/uuid-1');
    await waitFor(() => expect(lastLocation).toBe('/app/admin/users/uuid-1'));
  });

  it('translates the retired #shared alias onto Users', async () => {
    renderAdmin('/app/admin#shared');
    await waitFor(() => expect(lastLocation).toBe('/app/admin/users'));
  });

  it('sends an unrecognized hash to Overview', async () => {
    renderAdmin('/app/admin#nonsense');
    await waitFor(() => expect(lastLocation).toBe('/app/admin/overview'));
  });

  // Regression: an earlier version cleared the hash during render, so
  // StrictMode's second render pass saw nothing and the index route's
  // redirect to Overview won instead.
  it('survives a StrictMode double render', async () => {
    lastLocation = '';
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/app/admin#config']}>
          <LocationProbe />
          <Routes>
            <Route path="/app/admin/*" element={<AdminPanel />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    );
    await waitFor(() => expect(lastLocation).toBe('/app/admin/config'));
  });

  it('does not interpret a hash sitting on a real sub-route', async () => {
    renderAdmin('/app/admin/config#anchor');
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Config', level: 2 })).toBeInTheDocument();
    });
    expect(lastLocation).toBe('/app/admin/config');
  });
});

describe('AdminPanel Users section', () => {
  it('renders the user list', async () => {
    renderAdmin('/app/admin/users');
    // Each user appears twice: once in the desktop table, once in the mobile card list
    await waitFor(() => {
      expect(screen.getAllByText('alice@example.com').length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getAllByText('bob@example.com').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('2 users')).toBeInTheDocument();
  });

  it('renders an admin badge for users with role=admin', async () => {
    renderAdmin('/app/admin/users');
    await waitFor(() => expect(screen.getAllByText('alice@example.com').length).toBeGreaterThan(0));
    expect(screen.getAllByTitle('Platform admin').length).toBeGreaterThan(0);
  });

  it('navigates to the detail route when a row is clicked', async () => {
    renderAdmin('/app/admin/users');
    await waitFor(() => expect(screen.getAllByText('bob@example.com').length).toBeGreaterThan(0));

    const user = userEvent.setup();
    const bobCell = screen.getAllByText('bob@example.com').find(el => el.closest('tr'));
    await user.click(bobCell!.closest('button')!);

    await waitFor(() => expect(lastLocation).toBe('/app/admin/users/uuid-bob'));
  });

  it('opens a modal confirm (not window.confirm) for delete', async () => {
    const origConfirm = window.confirm;
    window.confirm = vi.fn();
    try {
      renderAdmin('/app/admin/users');
      await waitFor(() =>
        expect(screen.getAllByText('bob@example.com').length).toBeGreaterThan(0),
      );

      const user = userEvent.setup();
      const bobCell = screen.getAllByText('bob@example.com').find(el => el.closest('tr'));
      const bobRow = bobCell?.closest('tr') as HTMLElement;
      await user.click(within(bobRow).getByRole('button', { name: /row actions/i }));
      const deleteItems = await screen.findAllByRole('menuitem', { name: /delete user/i });
      await user.click(deleteItems[0]!);

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });
      expect(screen.getByText(/Delete bob@example.com/)).toBeInTheDocument();
      expect(window.confirm).not.toHaveBeenCalled();
    } finally {
      window.confirm = origConfirm;
    }
  });
});

describe('AdminPanel Overview', () => {
  it('renders the operational overview', async () => {
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: 'System and integration health' }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText('0.7.32')).toBeInTheDocument();
  });

  it('does not show growth or waitlist metrics', async () => {
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: 'System and integration health' }),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/Signups this/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waitlist/i)).not.toBeInTheDocument();
  });

  it('says so plainly when nothing needs attention', async () => {
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(screen.getByText(/Nothing needs attention/i)).toBeInTheDocument();
    });
  });

  it('flags a down dependency probe and links to Monitoring', async () => {
    const { getMonitoringStatus } = await import('./admin-api');
    vi.mocked(getMonitoringStatus).mockResolvedValueOnce({
      ...mocks.MOCK_MONITORING,
      health_monitor: {
        ...mocks.MOCK_MONITORING.health_monitor,
        probes: {
          database: {
            ...mocks.MOCK_MONITORING.health_monitor.probes.database,
            status: 'down',
            detail: 'connection refused',
          },
        },
      },
    } as never);
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(screen.getByText('Database is down')).toBeInTheDocument();
    });
    expect(screen.getByText('1 of 1 down')).toBeInTheDocument();
  });

  it('does not treat a never-connected probe as breakage', async () => {
    const { getMonitoringStatus } = await import('./admin-api');
    vi.mocked(getMonitoringStatus).mockResolvedValueOnce({
      ...mocks.MOCK_MONITORING,
      health_monitor: {
        ...mocks.MOCK_MONITORING.health_monitor,
        probes: {
          quickbooks: {
            label: 'QuickBooks',
            status: 'down',
            detail: 'not connected',
            consecutive_failures: 0,
            since: '2026-05-01T00:00:00Z',
            last_checked: '2026-05-01T00:00:00Z',
            never_connected: true,
          },
        },
      },
    } as never);
    renderAdmin('/app/admin/overview');
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'System and integration health' }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/QuickBooks is down/)).not.toBeInTheDocument();
  });

  it('keeps rendering when the monitoring endpoint fails', async () => {
    const { getMonitoringStatus } = await import('./admin-api');
    vi.mocked(getMonitoringStatus).mockRejectedValueOnce(new Error('probe timeout'));
    renderAdmin('/app/admin/overview');
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'System and integration health' }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
  });

  it('shows a stats error with Retry when the stats endpoint fails', async () => {
    const { getAdminStats } = await import('./admin-api');
    vi.mocked(getAdminStats).mockRejectedValueOnce(new Error('Network error'));
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('explains the shared-activity panel when nobody has opted in', async () => {
    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(
        screen.getByText(/No users have opted into research data sharing yet/i),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole('heading', { name: 'Shared activity' }),
    ).toBeInTheDocument();
  });

  it('lists consenting users with a direct route into their activity', async () => {
    const { getSharedDataSummary, getSharedDataUsers } = await import('./admin-api');
    vi.mocked(getSharedDataSummary).mockResolvedValueOnce({
      consenting_user_count: 2,
      consents_changed_this_week: 1,
      conversations_this_week: 12,
      heartbeats_this_week: 7,
      open_reports_count: 2,
      top_users_this_week: [
        { id: 'uuid-1', email: 'alice@example.com', user_id: 'google_alice', messages_this_week: 9 },
      ],
    });
    vi.mocked(getSharedDataUsers).mockResolvedValueOnce({
      total: 2,
      items: [
        {
          id: 'uuid-quiet',
          user_id: 'google_quiet',
          email: 'quiet@example.com',
          consent_at: '2026-04-02T00:00:00Z',
          conversation_count: 1,
          last_message_at: null,
        },
        {
          id: 'uuid-1',
          user_id: 'google_alice',
          email: 'alice@example.com',
          consent_at: '2026-04-01T00:00:00Z',
          conversation_count: 4,
          last_message_at: '2026-04-28T09:00:00Z',
        },
      ],
    });

    renderAdmin('/app/admin/overview');
    await waitFor(() => {
      expect(screen.getByText('alice@example.com')).toBeInTheDocument();
    });

    // Sorted by recent activity: Alice appears before a quiet user.
    const rows = screen.getAllByRole('row').filter(r => within(r).queryByText(/@example\.com/));
    expect(within(rows[0]!).getByText('alice@example.com')).toBeInTheDocument();

    expect(screen.getByText('Last message')).toBeInTheDocument();
    expect(screen.getByText('No messages yet')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(within(rows[0]!).getByRole('button', { name: 'Memory' }));
    await waitFor(() => expect(lastLocation).toBe('/app/admin/users/uuid-1/memory'));
  });
});
