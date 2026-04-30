import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import ChannelsPage from './ChannelsPage';

const mockGetChannelConfig = vi.fn();
const mockUpdateChannelConfig = vi.fn();
const mockGetChannelRoutes = vi.fn();
const mockToggleChannelRoute = vi.fn();
const mockGetTelegramLink = vi.fn();
const mockGetTelegramBotInfo = vi.fn();
const mockSetTelegramLink = vi.fn();
const mockGetLinqLink = vi.fn();
const mockSetLinqLink = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    updateChannelConfig: (...args: unknown[]) => mockUpdateChannelConfig(...args),
    getChannelRoutes: (...args: unknown[]) => mockGetChannelRoutes(...args),
    toggleChannelRoute: (...args: unknown[]) => mockToggleChannelRoute(...args),
    getTelegramLink: (...args: unknown[]) => mockGetTelegramLink(...args),
    getTelegramBotInfo: (...args: unknown[]) => mockGetTelegramBotInfo(...args),
    setTelegramLink: (...args: unknown[]) => mockSetTelegramLink(...args),
    getLinqLink: (...args: unknown[]) => mockGetLinqLink(...args),
    setLinqLink: (...args: unknown[]) => mockSetLinqLink(...args),
  },
}));

const mockProfile = {
  channel_identifier: '',
  preferred_channel: 'webchat',
};

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useOutletContext: () => ({
      profile: mockProfile,
    }),
  };
});

let mockIsPremium = true;

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User' },
    authConfig: { required: true, method: 'oidc' },
    isPremium: mockIsPremium,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockProfile.channel_identifier = '';
  mockProfile.preferred_channel = 'webchat';
  mockIsPremium = true;
  mockGetChannelConfig.mockResolvedValue({
    telegram_bot_token_set: true,
    telegram_allowed_chat_id: '*',
    linq_api_token_set: true,
    linq_from_number: '+15551234567',
    linq_allowed_numbers: '*',
    linq_preferred_service: 'iMessage',
    bluebubbles_configured: false,
    bluebubbles_allowed_numbers: '',
    imessage_backend: 'linq',
  });
  mockGetChannelRoutes.mockResolvedValue({ routes: [] });
  mockToggleChannelRoute.mockResolvedValue({ channel: 'telegram', channel_identifier: '123', enabled: true, created_at: '' });
  mockGetTelegramLink.mockResolvedValue({ telegram_user_id: '12345', connected: true });
  mockGetTelegramBotInfo.mockResolvedValue(null);
  mockSetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
  mockGetLinqLink.mockResolvedValue({ phone_number: '+15559876543', connected: true });
  mockSetLinqLink.mockResolvedValue({ phone_number: null, connected: false });
});

describe('ChannelsPage - Channel States', () => {
  it('renders Telegram and a single unified iMessage card when linq is the backend', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    // iMessage appears once as a unified card (Linq backend is not named in the UI).
    expect(screen.getAllByText('iMessage')).toHaveLength(1);
    expect(screen.queryByText(/Text Messaging/)).not.toBeInTheDocument();
    expect(screen.queryByText(/BlueBubbles/)).not.toBeInTheDocument();
  });

  it('renders the iMessage card when bluebubbles is the backend (still one card, still labeled iMessage)', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: true,
      bluebubbles_allowed_numbers: '*',
      imessage_backend: 'bluebubbles',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    expect(screen.getAllByText('iMessage')).toHaveLength(1);
    expect(screen.queryByText(/BlueBubbles/)).not.toBeInTheDocument();
  });

  it('shows the iMessage address and a "Waiting" badge when no inbound has arrived yet', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'linq', channel_identifier: '+15559876543', enabled: true, created_at: '', last_inbound_at: null },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Send an iMessage to/)).toBeInTheDocument();
    });
    expect(screen.getByText('+15551234567')).toBeInTheDocument();
    expect(screen.getByText(/Waiting for your first message/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Connection verified')).not.toBeInTheDocument();
  });

  it('shows a "Verified" badge once last_inbound_at is populated', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        {
          channel: 'linq',
          channel_identifier: '+15559876543',
          enabled: true,
          created_at: '',
          last_inbound_at: '2026-04-15T13:00:00Z',
        },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByLabelText('Connection verified')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Waiting for your first message/)).not.toBeInTheDocument();
  });

  it('hides the iMessage card entirely when no iMessage backend is configured', async () => {
    // Telegram available, neither iMessage backend configured -> iMessage card is filtered out.
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: null,
    });
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    expect(screen.queryByText('iMessage')).not.toBeInTheDocument();
    // Telegram shows "Setup needed" since the server has a bot token but the user lacks a chat id.
    expect(screen.getByText('Setup needed')).toBeInTheDocument();
  });

  it('shows "Setup needed" badge for available but unconfigured channels', async () => {
    // Telegram available (token set) but user has no chat ID configured
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: null,
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Setup needed')).toBeInTheDocument();
    });
  });

  it('shows "Ready" badge for configured but inactive channels', async () => {
    // Telegram configured (premium user with telegram_user_id) but no active route
    mockGetChannelRoutes.mockResolvedValue({ routes: [] });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Ready');
      // Both Telegram and Linq are configured (have premium link data) with no routes
      expect(badges.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('shows "Active" badge with check for active channel', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument();
    });
  });

  it('shows radio buttons only for configured/active channels plus None', async () => {
    // Telegram: active (configured + route), Linq: configured (has phone), BB: unavailable
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const radios = screen.getAllByRole('radio');
      // None + telegram (active) + linq (configured) = 3 radios
      expect(radios).toHaveLength(3);
    });
  });

  it('shows webchat always-available note', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/web chat is always available/i)).toBeInTheDocument();
    });
  });
});

describe('ChannelsPage - Channel Activation', () => {
  it('calls toggle endpoint when radio is clicked', async () => {
    // Both telegram and linq are configured; telegram is active
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      // None + telegram + linq = 3 radios
      expect(screen.getAllByRole('radio')).toHaveLength(3);
    });

    // Click linq radio to switch
    const linqRadio = screen.getByDisplayValue('linq');
    await user.click(linqRadio);

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('linq', true);
    });
  });

  it('deactivates channel when None is selected', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });
    mockToggleChannelRoute.mockResolvedValue({ channel: 'telegram', channel_identifier: '111', enabled: false, created_at: '' });

    renderWithRouter(<ChannelsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByDisplayValue('none')).toBeInTheDocument();
    });

    const noneRadio = screen.getByDisplayValue('none');
    await user.click(noneRadio);

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('telegram', false);
    });
  });
});

describe('ChannelsPage - Config Forms', () => {
  it('auto-expands config form for "available" channels', async () => {
    // Telegram is available (token set) but not configured (no user ID)
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: null,
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      // Config form auto-expands for the available channel
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });
  });

  it('shows settings toggle for configured/active channels', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const toggles = screen.getAllByText('Your settings');
      expect(toggles.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('shows bot info banner for premium Telegram when active', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });
    mockGetTelegramBotInfo.mockResolvedValue({ bot_username: 'my_cool_bot', bot_link: 'https://t.me/my_cool_bot' });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('@my_cool_bot')).toBeInTheDocument();
    });
  });
});

describe('ChannelsPage - Unavailable channels are hidden', () => {
  it('hides Telegram entirely when the bot token is not configured', async () => {
    // Per #1029 / #1040 we no longer render greyed-out unavailable channel
    // cards. Telegram disappears from the list when telegram_bot_token_set
    // is false; the user does not see a Telegram card at all (and so no
    // env var name can leak into user-facing copy).
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: 'linq',
    });
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });

    renderWithRouter(<ChannelsPage />);

    // iMessage card renders; Telegram does not.
    await waitFor(() => {
      expect(screen.queryByText('iMessage')).toBeInTheDocument();
    });
    expect(screen.queryByText('Telegram')).not.toBeInTheDocument();
    expect(screen.queryByText(/Contact your administrator to enable Telegram/)).not.toBeInTheDocument();
    expect(screen.queryByText(/TELEGRAM_BOT_TOKEN/)).not.toBeInTheDocument();
  });
});

describe('ChannelsPage - Empty state', () => {
  it('shows empty state when no channels are available', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: null,
    });
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('No messaging channels available')).toBeInTheDocument();
    });
    expect(screen.getByText('Go to Chat')).toBeInTheDocument();
  });
});

describe('ChannelsPage - OSS mode', () => {
  it('shows OSS telegram config form when not premium', async () => {
    mockIsPremium = false;
    // Telegram available but needs config
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: null,
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      // Config form auto-expands for available channel
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });
  });
});
