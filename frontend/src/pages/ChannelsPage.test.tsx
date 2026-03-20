import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import ChannelsPage from './ChannelsPage';

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

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User' },
    authConfig: { required: true, method: 'oidc' },
    isPremium: true,
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

  it('shows generic text when bot-info is not available', async () => {
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
      expect(screen.getByText(/send a message to the bot to connect/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/@/)).not.toBeInTheDocument();
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
