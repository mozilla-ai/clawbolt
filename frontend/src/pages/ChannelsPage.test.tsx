import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import ChannelsPage from './ChannelsPage';

const mockGetChannelConfig = vi.fn();
const mockUpdateChannelConfig = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    updateChannelConfig: (...args: unknown[]) => mockUpdateChannelConfig(...args),
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
  mockIsPremium = true;
  mockGetChannelConfig.mockResolvedValue({
    telegram_bot_token_set: true,
    telegram_allowed_chat_id: '*',
    linq_api_token_set: true,
    linq_from_number: '+15551234567',
    linq_allowed_numbers: '*',
    linq_preferred_service: 'iMessage',
  });
  vi.stubGlobal('fetch', vi.fn());
});

describe('ChannelsPage - PremiumTelegramSection', () => {
  it('shows bot info banner when bot-info endpoint returns data', async () => {
    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'my_cool_bot', bot_link: 'https://t.me/my_cool_bot' }),
        });
      }
      // Telegram link data
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

  it('shows bot username in not-connected message when available', async () => {
    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'helper_bot', bot_link: 'https://t.me/helper_bot' }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ telegram_user_id: '123', connected: false }),
      });
    });
    vi.stubGlobal('fetch', mockFetch);

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('@helper_bot', { exact: false })).toBeInTheDocument();
    });
  });
});

describe('ChannelsPage - disabled state when channels not configured', () => {
  it('disables Telegram user ID input when bot token is not set (premium)', async () => {
    mockIsPremium = true;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '*',
      linq_preferred_service: 'iMessage',
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.includes('/api/channels/linq')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ phone_number: null, connected: false }),
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
      expect(screen.getByText('Not configured')).toBeInTheDocument();
    });

    const telegramInput = screen.getByPlaceholderText('e.g. 123456789');
    expect(telegramInput).toBeDisabled();
  });

  it('enables Telegram user ID input when bot token is set (premium)', async () => {
    mockIsPremium = true;

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'test_bot', bot_link: 'https://t.me/test_bot' }),
        });
      }
      if (url.includes('/api/channels/linq')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ phone_number: null, connected: false }),
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
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Not configured');
      expect(badges.length).toBeGreaterThanOrEqual(1);
    });

    const telegramInput = screen.getByPlaceholderText('e.g. 123456789');
    expect(telegramInput).toBeDisabled();
  });

  it('disables Linq fields when API token is not set (OSS)', async () => {
    mockIsPremium = false;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText('Not configured')).toBeInTheDocument();
    });

    const phoneInput = screen.getByPlaceholderText('e.g. +15551234567');
    expect(phoneInput).toBeDisabled();

    const serviceSelect = screen.getByLabelText('Preferred messaging service');
    expect(serviceSelect).toHaveAttribute('data-disabled', 'true');
  });

  it('enables Linq fields when API token is set (OSS)', async () => {
    mockIsPremium = false;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: true,
      linq_from_number: '+15551234567',
      linq_allowed_numbers: '*',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      const badges = screen.getAllByText('Connected');
      expect(badges.length).toBeGreaterThanOrEqual(1);
    });

    const phoneInput = screen.getByPlaceholderText('e.g. +15551234567');
    expect(phoneInput).not.toBeDisabled();

    const serviceSelect = screen.getByLabelText('Preferred messaging service');
    expect(serviceSelect).not.toHaveAttribute('data-disabled');
  });

  it('disables phone number input when Linq is not configured (premium)', async () => {
    mockIsPremium = true;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('bot-info')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ bot_username: 'test_bot', bot_link: 'https://t.me/test_bot' }),
        });
      }
      if (url.includes('/api/channels/linq')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ phone_number: null, connected: false }),
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
      expect(screen.getByText('Not configured')).toBeInTheDocument();
    });

    const phoneInput = screen.getByPlaceholderText('e.g. +15551234567');
    expect(phoneInput).toBeDisabled();
  });

  it('shows setup hint for Telegram in OSS mode when not configured', async () => {
    mockIsPremium = false;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/TELEGRAM_BOT_TOKEN/)).toBeInTheDocument();
    });
  });

  it('shows setup hint for Linq in OSS mode when not configured', async () => {
    mockIsPremium = false;
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '*',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<ChannelsPage />);

    await waitFor(() => {
      expect(screen.getByText(/LINQ_API_TOKEN/)).toBeInTheDocument();
    });
  });
});
