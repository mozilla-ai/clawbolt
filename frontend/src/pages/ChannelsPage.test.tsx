import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import ChannelsPage from './ChannelsPage';

const mockGetChannelConfig = vi.fn();
const mockUpdateChannelConfig = vi.fn();
const mockGetChannelRoutes = vi.fn();
const mockToggleChannelRoute = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    updateChannelConfig: (...args: unknown[]) => mockUpdateChannelConfig(...args),
    getChannelRoutes: (...args: unknown[]) => mockGetChannelRoutes(...args),
    toggleChannelRoute: (...args: unknown[]) => mockToggleChannelRoute(...args),
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

vi.mock('@/lib/api-client', () => ({
  getAccessToken: () => 'test-token',
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
  vi.stubGlobal('fetch', vi.fn());
});

describe('ChannelsPage - Radio Selector', () => {
  it('renders three channel radio options', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
    expect(screen.getByText('Text Messaging (iMessage / RCS / SMS)')).toBeInTheDocument();
    expect(screen.getByText('BlueBubbles (iMessage)')).toBeInTheDocument();
  });

  it('shows radio inputs for channel selection', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const radios = screen.getAllByRole('radio');
      expect(radios).toHaveLength(3);
    });
  });

  it('shows webchat always-available note', async () => {
    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/web chat is always available/i)).toBeInTheDocument();
    });
  });

  it('selects channel and calls toggle endpoint on radio change', async () => {
    renderWithRouter(<ChannelsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });

    const telegramRadio = screen.getByDisplayValue('telegram');
    await user.click(telegramRadio);

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('telegram', true);
    });
  });

  it('shows config section for selected channel', async () => {
    mockProfile.preferred_channel = 'telegram';
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'test_bot', bot_link: 'https://t.me/test_bot' }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: null, connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram Configuration')).toBeInTheDocument();
    });
  });

  it('shows Connected badge for channels with routes', async () => {
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Connected')).toBeInTheDocument();
    });
  });

  it('shows Not configured badge for channels without routes', async () => {
    mockGetChannelRoutes.mockResolvedValue({ routes: [] });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Not configured');
      expect(badges.length).toBe(3);
    });
  });

  it('does not show config section when no channel is selected', async () => {
    mockProfile.preferred_channel = 'webchat';
    mockGetChannelRoutes.mockResolvedValue({ routes: [] });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });

    expect(screen.queryByText('Telegram Configuration')).not.toBeInTheDocument();
    expect(screen.queryByText('Text Messaging Configuration')).not.toBeInTheDocument();
    expect(screen.queryByText('BlueBubbles Configuration')).not.toBeInTheDocument();
  });
});

describe('ChannelsPage - PremiumTelegramSection via radio', () => {
  it('shows bot info banner when bot-info endpoint returns data', async () => {
    mockProfile.preferred_channel = 'telegram';
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'my_cool_bot', bot_link: 'https://t.me/my_cool_bot' }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: null, connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('@my_cool_bot')).toBeInTheDocument();
    });
  });

  it('shows not-configured message when bot token is not set', async () => {
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
    mockProfile.preferred_channel = 'telegram';
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: null, connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/a telegram bot token must be configured/i)).toBeInTheDocument();
    });
  });
});

describe('ChannelsPage - disabled state when channels not configured', () => {
  it('disables Telegram user ID input when bot token is not set (premium)', async () => {
    mockIsPremium = true;
    mockProfile.preferred_channel = 'telegram';
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '*',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
    });
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: null, connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });

    const telegramInput = screen.getByPlaceholderText('e.g. 123456789');
    expect(telegramInput).toBeDisabled();
  });

  it('enables Telegram user ID input when bot token is set (premium)', async () => {
    mockIsPremium = true;
    mockProfile.preferred_channel = 'telegram';
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'test_bot', bot_link: 'https://t.me/test_bot' }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: null, connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const telegramInput = screen.getByPlaceholderText('e.g. 123456789');
      expect(telegramInput).not.toBeDisabled();
    });
  });

  it('disables Telegram user ID input when bot token is not set (OSS)', async () => {
    mockIsPremium = false;
    mockProfile.preferred_channel = 'telegram';
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
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });

    const telegramInput = screen.getByPlaceholderText('e.g. 123456789');
    expect(telegramInput).toBeDisabled();
  });

  it('shows setup hint for Telegram in OSS mode when not configured', async () => {
    mockIsPremium = false;
    mockProfile.preferred_channel = 'telegram';
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
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'telegram', channel_identifier: '111', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/TELEGRAM_BOT_TOKEN/)).toBeInTheDocument();
    });
  });

  it('shows setup hint for Linq in OSS mode when not configured', async () => {
    mockIsPremium = false;
    mockProfile.preferred_channel = 'linq';
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
    mockGetChannelRoutes.mockResolvedValue({
      routes: [
        { channel: 'linq', channel_identifier: '+15551234567', enabled: true, created_at: '' },
      ],
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/LINQ_API_TOKEN/)).toBeInTheDocument();
    });
  });
});
