import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import DashboardPage from './DashboardPage';

const mockNavigate = vi.fn();

const mockGetChannelRoutes = vi.fn();
const mockGetChannelConfig = vi.fn();
const mockGetToolConfig = vi.fn();
const mockUpdateToolConfig = vi.fn();
const mockGetOAuthStatus = vi.fn();
const mockGetCalendarConfig = vi.fn();
const mockGetMemory = vi.fn();
const mockGetModelConfig = vi.fn();
const mockUpdateProfile = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getProfile: vi.fn().mockResolvedValue({}),
    getChannelRoutes: (...args: unknown[]) => mockGetChannelRoutes(...args),
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    getToolConfig: (...args: unknown[]) => mockGetToolConfig(...args),
    updateToolConfig: (...args: unknown[]) => mockUpdateToolConfig(...args),
    getOAuthStatus: (...args: unknown[]) => mockGetOAuthStatus(...args),
    getCalendarConfig: (...args: unknown[]) => mockGetCalendarConfig(...args),
    getMemory: (...args: unknown[]) => mockGetMemory(...args),
    getModelConfig: (...args: unknown[]) => mockGetModelConfig(...args),
    updateProfile: (...args: unknown[]) => mockUpdateProfile(...args),
  },
}));

const mockProfile = {
  id: '1',
  user_id: 'local@clawbolt.local',
  phone: '555-0100',
  timezone: 'America/Los_Angeles',
  soul_text: 'You are a helpful contractor assistant.',
  user_text: 'John is a plumber in Portland.',
  heartbeat_text: '- [ ] Follow up with client about kitchen remodel',
  preferred_channel: 'telegram',
  channel_identifier: '',
  heartbeat_opt_in: true,
  heartbeat_frequency: 'daily',
  heartbeat_max_daily: 0,
  onboarding_complete: true,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User' },
    authConfig: { required: true, method: 'oidc' },
    isPremium: false,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useOutletContext: () => ({ profile: mockProfile, reloadProfile: vi.fn() }),
    useNavigate: () => mockNavigate,
  };
});

function setupMocks(overrides?: {
  routes?: unknown;
  channelConfig?: unknown;
  tools?: unknown;
  oauth?: unknown;
  calendarConfig?: unknown;
  memory?: unknown;
  modelConfig?: unknown;
}) {
  mockGetChannelRoutes.mockResolvedValue(
    overrides?.routes ?? {
      routes: [{ channel: 'telegram', channel_identifier: '123', enabled: true, created_at: '' }],
    },
  );
  mockGetChannelConfig.mockResolvedValue(
    overrides?.channelConfig ?? {
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '*',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    },
  );
  mockGetToolConfig.mockResolvedValue(
    overrides?.tools ?? {
      tools: [
        { name: 'workspace', description: '', category: 'core', enabled: true, domain_group: '', domain_group_order: 0 },
        {
          name: 'calendar', description: '', category: 'domain', enabled: true, domain_group: '', domain_group_order: 0,
          sub_tools: [
            { name: 'calendar_list_events', description: '', enabled: true, permission_level: 'auto' },
            { name: 'calendar_create_event', description: '', enabled: true, permission_level: 'ask' },
            { name: 'calendar_update_event', description: '', enabled: false, permission_level: 'ask' },
          ],
        },
      ],
    },
  );
  mockGetOAuthStatus.mockResolvedValue(
    overrides?.oauth ?? {
      integrations: [{ integration: 'google_calendar', connected: true, configured: true }],
    },
  );
  mockGetCalendarConfig.mockResolvedValue(
    overrides?.calendarConfig ?? {
      calendars: [
        { calendar_id: 'primary', display_name: 'Work', disabled_tools: ['calendar_delete_event'], access_role: 'owner' },
        { calendar_id: 'secondary', display_name: 'Personal', disabled_tools: [], access_role: 'owner' },
      ],
    },
  );
  mockGetMemory.mockResolvedValue(
    overrides?.memory ?? { content: 'John is a plumber. He lives in Portland.' },
  );
  mockGetModelConfig.mockResolvedValue(
    overrides?.modelConfig ?? {
      llm_provider: 'anthropic',
      llm_model: 'claude-sonnet-4-6',
      llm_api_base: null,
      vision_model: 'claude-sonnet-4-6',
      vision_provider: 'anthropic',
      heartbeat_model: 'claude-sonnet-4-6',
      heartbeat_provider: 'anthropic',
      compaction_model: 'claude-sonnet-4-6',
      compaction_provider: 'anthropic',
    },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockProfile.heartbeat_opt_in = true;
  mockProfile.heartbeat_frequency = 'daily';
  mockProfile.heartbeat_text = '- [ ] Follow up with client about kitchen remodel';
  mockProfile.soul_text = 'You are a helpful contractor assistant.';
  mockProfile.user_text = 'John is a plumber in Portland.';
  mockProfile.timezone = 'America/Los_Angeles';
});

describe('DashboardPage', () => {
  it('renders all 7 cards with descriptions', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    expect(screen.getByText('Dashboard')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('Channels')).toBeInTheDocument();
    });
    expect(screen.getByText('Tools')).toBeInTheDocument();
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('Heartbeat')).toBeInTheDocument();
    expect(screen.getByText('Soul')).toBeInTheDocument();
    expect(screen.getByText('User')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();

    // Descriptions are present
    expect(screen.getByText('Messaging platforms your assistant listens on.')).toBeInTheDocument();
    expect(screen.getByText("Long-term facts your assistant has learned about your business.")).toBeInTheDocument();
  });

  it('shows per-channel status lines with Active for active channel', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    // Telegram should show "Active"
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('shows per-channel status lines for all channels', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    expect(screen.getByText('Text Messaging (iMessage / RCS / SMS)')).toBeInTheDocument();
    expect(screen.getByText('BlueBubbles (iMessage)')).toBeInTheDocument();
  });

  it('shows "Setup needed" for available but unconfigured channels', async () => {
    setupMocks({
      channelConfig: {
        telegram_bot_token_set: true,
        telegram_allowed_chat_id: '',
        linq_api_token_set: true,
        linq_from_number: '+15551234567',
        linq_allowed_numbers: '',
        linq_preferred_service: 'iMessage',
        bluebubbles_configured: false,
        bluebubbles_allowed_numbers: '',
      },
      routes: { routes: [] },
    });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Setup needed');
      expect(badges.length).toBe(2);
    });
  });

  it('shows "Not available" for channels without server config', async () => {
    setupMocks({
      channelConfig: {
        telegram_bot_token_set: true,
        telegram_allowed_chat_id: '*',
        linq_api_token_set: false,
        linq_from_number: '',
        linq_allowed_numbers: '',
        linq_preferred_service: 'iMessage',
        bluebubbles_configured: false,
        bluebubbles_allowed_numbers: '',
      },
    });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Not available');
      expect(badges.length).toBe(2);
    });
  });

  it('shows setup prompt when no channels are available at all', async () => {
    setupMocks({
      routes: { routes: [] },
      channelConfig: {
        telegram_bot_token_set: false,
        telegram_allowed_chat_id: '',
        linq_api_token_set: false,
        linq_from_number: '',
        linq_allowed_numbers: '',
        linq_preferred_service: 'iMessage',
        bluebubbles_configured: false,
        bluebubbles_allowed_numbers: '',
      },
    });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText(/Set up a messaging channel/)).toBeInTheDocument();
    });
  });

  it('shows domain tool toggles only when connected', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Google Calendar')).toBeInTheDocument();
    });
    // Calendar is connected, so toggle appears
    expect(screen.getByLabelText('Toggle Google Calendar')).toBeInTheDocument();
    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('shows "Not connected" and hides toggle for unconnected tools', async () => {
    setupMocks({
      tools: {
        tools: [
          { name: 'workspace', description: '', category: 'core', enabled: true, domain_group: '', domain_group_order: 0 },
          { name: 'calendar', description: '', category: 'domain', enabled: true, domain_group: '', domain_group_order: 0 },
          { name: 'quickbooks', description: '', category: 'domain', enabled: true, domain_group: '', domain_group_order: 0 },
        ],
      },
      oauth: {
        integrations: [
          { integration: 'google_calendar', connected: true, configured: true },
          { integration: 'quickbooks', connected: false, configured: true },
        ],
      },
    });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('QuickBooks')).toBeInTheDocument();
    });
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByLabelText('Toggle QuickBooks')).not.toBeInTheDocument();
    // Calendar IS connected, so its toggle is present
    expect(screen.getByLabelText('Toggle Google Calendar')).toBeInTheDocument();
  });

  it('shows per-calendar names and capability counts for connected calendar', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Work')).toBeInTheDocument();
    });
    expect(screen.getByText('Personal')).toBeInTheDocument();
    // Work has 1 disabled tool out of 4 per-calendar tools → 3/4
    expect(screen.getByText('3/4')).toBeInTheDocument();
    // Personal has 0 disabled → 4/4
    expect(screen.getByText('4/4')).toBeInTheDocument();
  });

  it('shows memory content preview and word count', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('John is a plumber. He lives in Portland.')).toBeInTheDocument();
    });
    expect(screen.getByText('8 words')).toBeInTheDocument();
  });

  it('shows setup prompt when memory is empty', async () => {
    setupMocks({ memory: { content: '' } });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText(/Chat with your assistant to build up knowledge/)).toBeInTheDocument();
    });
  });

  it('shows heartbeat frequency badge and text preview', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('daily')).toBeInTheDocument();
    });
    expect(screen.getByText(/Follow up with client about kitchen remodel/)).toBeInTheDocument();
  });

  it('shows setup prompt when heartbeat is off', async () => {
    mockProfile.heartbeat_opt_in = false;
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText(/Enable to let your assistant proactively/)).toBeInTheDocument();
    });
  });

  it('renders heartbeat toggle switch', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByLabelText('Toggle heartbeat check-ins')).toBeInTheDocument();
    });
  });

  it('heartbeat toggle calls updateProfile', async () => {
    mockProfile.heartbeat_opt_in = true;
    mockUpdateProfile.mockResolvedValue({});
    setupMocks();
    const user = userEvent.setup();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByLabelText('Toggle heartbeat check-ins')).toBeInTheDocument();
    });

    const toggle = screen.getByLabelText('Toggle heartbeat check-ins');
    await user.click(toggle);

    await waitFor(() => {
      expect(mockUpdateProfile).toHaveBeenCalled();
    });
    expect(mockUpdateProfile.mock.calls[0]![0]).toEqual({ heartbeat_opt_in: false });
  });

  it('tool toggle calls updateToolConfig', async () => {
    mockUpdateToolConfig.mockResolvedValue({});
    setupMocks();
    const user = userEvent.setup();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByLabelText('Toggle Google Calendar')).toBeInTheDocument();
    });

    const toggle = screen.getByLabelText('Toggle Google Calendar');
    await user.click(toggle);

    await waitFor(() => {
      expect(mockUpdateToolConfig).toHaveBeenCalledWith([{ name: 'calendar', enabled: false }]);
    });
  });

  it('shows soul text preview', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('You are a helpful contractor assistant.')).toBeInTheDocument();
    });
  });

  it('shows setup prompt for empty soul text', async () => {
    mockProfile.soul_text = '';
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText(/Define how your assistant should behave/)).toBeInTheDocument();
    });
  });

  it('shows user text preview with timezone', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('John is a plumber in Portland.')).toBeInTheDocument();
    });
    expect(screen.getByText('America/Los_Angeles')).toBeInTheDocument();
  });

  it('shows settings model and provider', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('claude-sonnet-4-6')).toBeInTheDocument();
    });
    expect(screen.getByText('anthropic')).toBeInTheDocument();
  });

  it('shows vision model when different from main model', async () => {
    setupMocks({
      modelConfig: {
        llm_provider: 'anthropic',
        llm_model: 'claude-sonnet-4-6',
        llm_api_base: null,
        vision_model: 'gpt-4o',
        vision_provider: 'openai',
        heartbeat_model: 'claude-sonnet-4-6',
        heartbeat_provider: 'anthropic',
        compaction_model: 'claude-sonnet-4-6',
        compaction_provider: 'anthropic',
      },
    });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('gpt-4o')).toBeInTheDocument();
    });
  });

  it('shows green dot for configured cards', async () => {
    setupMocks();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      const greenDots = screen.getAllByLabelText('Configured');
      expect(greenDots.length).toBeGreaterThan(0);
    });
  });

  it('shows amber dot for needs-attention cards', async () => {
    mockProfile.heartbeat_opt_in = false;
    mockProfile.soul_text = '';
    mockProfile.user_text = '';
    setupMocks({ routes: { routes: [] }, memory: { content: '' } });
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      const amberDots = screen.getAllByLabelText('Needs attention');
      expect(amberDots.length).toBeGreaterThan(0);
    });
  });

  it('navigates to correct route when card is clicked', async () => {
    setupMocks();
    const user = userEvent.setup();
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Channels')).toBeInTheDocument();
    });

    const channelsCard = screen.getByText('Channels').closest('[tabindex]');
    if (channelsCard) {
      await user.click(channelsCard);
      expect(mockNavigate).toHaveBeenCalledWith('/app/channels');
    }
  });

  it('shows per-card error when one API fails', async () => {
    mockGetChannelRoutes.mockRejectedValue(new Error('Network error'));
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    });
    mockGetToolConfig.mockResolvedValue({ tools: [] });
    mockGetOAuthStatus.mockResolvedValue({ integrations: [] });
    mockGetMemory.mockResolvedValue({ content: 'some memory content here' });
    mockGetModelConfig.mockResolvedValue({
      llm_provider: 'anthropic',
      llm_model: 'claude-sonnet-4-6',
      llm_api_base: null,
      vision_model: '',
      vision_provider: '',
      heartbeat_model: '',
      heartbeat_provider: '',
      compaction_model: '',
      compaction_provider: '',
    });

    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText('Unable to load')).toBeInTheDocument();
    });
    // Other cards still render
    expect(screen.getByText('some memory content here')).toBeInTheDocument();
  });
});
