import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import GetStartedPage from './GetStartedPage';

const mockNavigate = vi.fn();
const mockReloadProfile = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useOutletContext: () => ({
      profile: { onboarding_complete: false, preferred_channel: 'webchat' },
      reloadProfile: mockReloadProfile,
      isPremium: false,
      isAdmin: false,
    }),
  };
});

let mockIsPremium = false;

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User' },
    authConfig: { required: false },
    isPremium: mockIsPremium,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

const mockUpdateProfile = vi.fn().mockResolvedValue({ onboarding_complete: true });
const mockUpdateChannelConfig = vi.fn().mockResolvedValue({
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: true,
  linq_from_number: '+15559876543',
  linq_allowed_numbers: '+15551234567',
  linq_preferred_service: 'iMessage',
  bluebubbles_configured: false,
  bluebubbles_allowed_numbers: '',
  imessage_backend: 'linq',
});
const mockGetChannelConfig = vi.fn().mockResolvedValue({
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: true,
  linq_from_number: '+15559876543',
  linq_allowed_numbers: '',
  linq_preferred_service: 'iMessage',
  bluebubbles_configured: false,
  bluebubbles_allowed_numbers: '',
  imessage_backend: 'linq',
});
const mockGetChannelRoutes = vi.fn().mockResolvedValue({ routes: [] });
const mockToggleChannelRoute = vi.fn().mockResolvedValue({
  channel: 'linq', channel_identifier: '', enabled: true, created_at: '',
});

vi.mock('@/api', () => ({
  default: {
    updateProfile: (...args: unknown[]) => mockUpdateProfile(...args),
    getProfile: vi.fn().mockResolvedValue({ onboarding_complete: false }),
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    updateChannelConfig: (...args: unknown[]) => mockUpdateChannelConfig(...args),
    getChannelRoutes: (...args: unknown[]) => mockGetChannelRoutes(...args),
    toggleChannelRoute: (...args: unknown[]) => mockToggleChannelRoute(...args),
    getTelegramLink: vi.fn().mockResolvedValue({ telegram_user_id: null, connected: false }),
    getLinqLink: vi.fn().mockResolvedValue({ phone_number: null, connected: false }),
    setLinqLink: vi.fn().mockResolvedValue({ phone_number: '+15551234567', connected: true }),
    setTelegramLink: vi.fn().mockResolvedValue({ telegram_user_id: null, connected: false }),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockIsPremium = false;
});

describe('GetStartedPage', () => {
  it('renders the get started heading and channel selection step', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(screen.getByText('Choose your messaging channel')).toBeInTheDocument();
    expect(screen.getByText('Send a message')).toBeInTheDocument();
    expect(screen.getByText("You're off to the races")).toBeInTheDocument();
  });

  it('renders channel selection radio options: Telegram, iMessage, None', async () => {
    renderWithRouter(<GetStartedPage />);

    // Wait for channel config to load so getVisibleChannels can filter the list.
    await waitFor(() => {
      expect(screen.getAllByText('iMessage')).toHaveLength(1);
    });
    expect(screen.getByText('Telegram')).toBeInTheDocument();
    expect(screen.queryByText(/Text Messaging/)).not.toBeInTheDocument();
    expect(screen.queryByText(/BlueBubbles/)).not.toBeInTheDocument();
    expect(screen.getByText('None')).toBeInTheDocument();
  });

  it('renders the dismiss button defaulting to chat when no channel selected', () => {
    renderWithRouter(<GetStartedPage />);
    expect(screen.getByText('Got it, take me to chat')).toBeInTheDocument();
  });

  it('shows dashboard dismiss button when a messaging channel is selected', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const linqRadio = await screen.findByDisplayValue('linq');
    await user.click(linqRadio);

    await waitFor(() => {
      expect(screen.getByText('Got it, take me to the dashboard')).toBeInTheDocument();
    });
  });

  it('shows "Configure your channel" placeholder when no channel is selected', () => {
    renderWithRouter(<GetStartedPage />);
    expect(screen.getByText('Configure your channel')).toBeInTheDocument();
    expect(screen.getByText('Select a channel above to configure it.')).toBeInTheDocument();
  });

  it('shows the shared OSS linq config form when text messaging is selected', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const linqRadio = await screen.findByDisplayValue('linq');
    await user.click(linqRadio);

    await waitFor(() => {
      expect(screen.getByText(/Configure iMessage/)).toBeInTheDocument();
    });
    // The shared OssLinqForm shows "Allowed Phone Number" field
    expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
  });

  it('shows the shared telegram config form when telegram is selected', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15559876543',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: 'linq',
    });

    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByDisplayValue('telegram')).not.toBeDisabled();
    });

    await user.click(screen.getByDisplayValue('telegram'));

    await waitFor(() => {
      expect(screen.getByText(/Configure Telegram/)).toBeInTheDocument();
    });
    // The shared OssTelegramForm shows "Your Telegram User ID" field
    expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
  });

  it('saves linq config via the shared form (updateChannelConfig)', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const linqRadio = await screen.findByDisplayValue('linq');
    await user.click(linqRadio);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({ linq_allowed_numbers: '+15551234567' });
    });
  });

  it('shows QR code and from-number when linq is configured and text messaging selected', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const linqRadio = await screen.findByDisplayValue('linq');
    await user.click(linqRadio);

    await waitFor(() => {
      // From-number appears in both the config form and Step 3
      const matches = screen.getAllByText('+15559876543');
      expect(matches.length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText(/Send an iMessage to this address to get started/)).toBeInTheDocument();
  });

  it('shows fallback messaging when no iMessage backend is configured', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
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

    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByText(/No messaging channel is configured yet/)).toBeInTheDocument();
    });
  });

  it('calls toggleChannelRoute when selecting a channel', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15559876543',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: 'linq',
    });

    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByDisplayValue('telegram')).not.toBeDisabled();
    });

    await user.click(screen.getByDisplayValue('telegram'));

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('telegram', true);
    });
  });

  it('renders a clickable "None" option for web chat only', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const noneRadio = screen.getByDisplayValue('none');
    expect(noneRadio).toBeInTheDocument();
    expect(noneRadio).not.toBeDisabled();

    await user.click(noneRadio);

    await waitFor(() => {
      expect(screen.getByText('No setup needed')).toBeInTheDocument();
    });
    expect(screen.getByText('Use the chat in the sidebar to talk to your assistant.')).toBeInTheDocument();
    // "None" keeps the chat-oriented dismiss button
    expect(screen.getByText('Got it, take me to chat')).toBeInTheDocument();
  });

  it('pre-populates selection from active channel route', async () => {
    mockGetChannelConfig.mockResolvedValue({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '123',
      linq_api_token_set: true,
      linq_from_number: '+15559876543',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      imessage_backend: 'linq',
    });
    mockGetChannelRoutes.mockResolvedValue({
      routes: [{ channel: 'telegram', channel_identifier: '123', enabled: true, created_at: '' }],
    });

    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      const telegramRadio = screen.getByDisplayValue('telegram') as HTMLInputElement;
      expect(telegramRadio.checked).toBe(true);
    });
    // Should show the telegram config form since it's pre-selected
    expect(screen.getByText(/Configure Telegram/)).toBeInTheDocument();
  });
});
