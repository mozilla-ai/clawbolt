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
  it('renders all three channel cards', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    expect(screen.getByText('Text Messaging (iMessage / RCS / SMS)')).toBeInTheDocument();
    expect(screen.getByText('BlueBubbles (iMessage)')).toBeInTheDocument();
  });

  it('shows "Not available" badge for unavailable channels alongside available ones', async () => {
    // Telegram available, Linq and BB unavailable
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    });
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Not available');
      expect(badges.length).toBe(2); // Linq and BB
    });
    // Telegram should show Setup needed
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

describe('ChannelsPage - Unavailable hints', () => {
  it('shows environment variable hint for unavailable Telegram', async () => {
    // Telegram unavailable, but Linq available so page renders channel cards (not empty state)
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    });
    mockGetTelegramLink.mockResolvedValue({ telegram_user_id: null, connected: false });
    mockGetLinqLink.mockResolvedValue({ phone_number: null, connected: false });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/TELEGRAM_BOT_TOKEN/)).toBeInTheDocument();
    });
  });

  it('shows environment variable hint for unavailable Linq', async () => {
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

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/LINQ_API_TOKEN/)).toBeInTheDocument();
    });
  });

  it('shows environment variable hint for unavailable BlueBubbles', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '*',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/BLUEBUBBLES_SERVER_URL/)).toBeInTheDocument();
    });
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
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      // Config form auto-expands for available channel
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });
  });
});
