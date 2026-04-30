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
    getTelegramBotInfo: vi.fn().mockResolvedValue({ bot_username: 'clawbolt_bot', bot_link: 'https://t.me/clawbolt_bot' }),
    getLinqLink: vi.fn().mockResolvedValue({ phone_number: null, connected: false }),
    setLinqLink: vi.fn().mockResolvedValue({ phone_number: '+15551234567', connected: true }),
    setTelegramLink: vi.fn().mockResolvedValue({ telegram_user_id: '12345', connected: true }),
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

  it('renders channel selection radio options for configured channels only', async () => {
    // Default fixture has telegram_bot_token_set=false and imessage_backend=linq.
    // After issues #1029 and #1040, Telegram is hidden when not configured.
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getAllByText('iMessage')).toHaveLength(1);
    });
    expect(screen.queryByText('Telegram')).not.toBeInTheDocument();
    expect(screen.queryByText(/Text Messaging/)).not.toBeInTheDocument();
    expect(screen.queryByText(/BlueBubbles/)).not.toBeInTheDocument();
    expect(screen.getByText('None')).toBeInTheDocument();
  });

  it('shows Telegram as a radio option when the bot token is set', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
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

    await waitFor(() => {
      expect(screen.getByText('Telegram')).toBeInTheDocument();
    });
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

// ---------------------------------------------------------------------------
// Mobile single-screen flow
// ---------------------------------------------------------------------------

const mockMatchMediaMobile = (mobile: boolean) => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(max-width: 640px)' ? mobile : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
};

describe('GetStartedPage (mobile flow)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsPremium = false;
    mockMatchMediaMobile(true);
  });

  it('renders the single-screen mobile layout instead of the 4-step wizard', async () => {
    renderWithRouter(<GetStartedPage />);
    await waitFor(() => {
      expect(screen.getByText("Hey, I'm Clawbolt")).toBeInTheDocument();
    });
    // Wizard step copy must NOT be present on mobile.
    expect(screen.queryByText('Choose your messaging channel')).not.toBeInTheDocument();
    expect(screen.queryByText("You're off to the races")).not.toBeInTheDocument();
    // Single-screen affordances.
    expect(screen.getByPlaceholderText('(555) 123-4567')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Text Clawbolt' })).toBeInTheDocument();
  });

  it('shows phone validation error when format is bad', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('(555) 123-4567');
    await user.type(input, 'abc');
    await user.click(screen.getByRole('button', { name: 'Text Clawbolt' }));
    await waitFor(() => {
      expect(screen.getByText(/Use a phone number like/)).toBeInTheDocument();
    });
    // Server-side calls must NOT have been attempted on a bad value.
    expect(mockUpdateChannelConfig).not.toHaveBeenCalled();
  });

  it('OSS persists phone via updateChannelConfig (linq_allowed_numbers)', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('(555) 123-4567');
    await user.type(input, '5551234567');
    await user.click(screen.getByRole('button', { name: 'Text Clawbolt' }));
    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({
        linq_allowed_numbers: '+15551234567',
      });
    });
  });

  it('OSS bluebubbles persists via updateChannelConfig (bluebubbles_allowed_numbers)', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: true,
      bluebubbles_allowed_numbers: '',
      bluebubbles_imessage_address: '+15559876543',
      imessage_backend: 'bluebubbles',
    });
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('(555) 123-4567');
    await user.type(input, '5551234567');
    await user.click(screen.getByRole('button', { name: 'Text Clawbolt' }));
    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({
        bluebubbles_allowed_numbers: '+15551234567',
      });
    });
  });

  it('shows empty-state copy when no iMessage backend is configured', async () => {
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
      expect(
        screen.getByText(/No messaging channels are configured on the server yet/),
      ).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Open web chat' })).toBeInTheDocument();
  });

  it('renders the Telegram flow when only Telegram is configured', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      bluebubbles_imessage_address: '',
      imessage_backend: null,
    });
    renderWithRouter(<GetStartedPage />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText('123456789')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Open Telegram' })).toBeInTheDocument();
    // No iMessage form when Telegram is the only option.
    expect(screen.queryByPlaceholderText('(555) 123-4567')).not.toBeInTheDocument();
    // No tab toggle either.
    expect(screen.queryByRole('tab', { name: 'iMessage' })).not.toBeInTheDocument();
  });

  it('shows the channel toggle and defaults to iMessage when both are configured', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: true,
      linq_from_number: '+15559876543',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      bluebubbles_imessage_address: '',
      imessage_backend: 'linq',
    });
    renderWithRouter(<GetStartedPage />);
    // Both tabs visible; iMessage active by default.
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'iMessage', selected: true })).toBeInTheDocument();
    });
    expect(screen.getByRole('tab', { name: 'Telegram', selected: false })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('(555) 123-4567')).toBeInTheDocument();
    // Click Telegram tab; iMessage form goes away, Telegram form appears.
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Telegram' }));
    await waitFor(() => {
      expect(screen.getByPlaceholderText('123456789')).toBeInTheDocument();
    });
    expect(screen.queryByPlaceholderText('(555) 123-4567')).not.toBeInTheDocument();
  });

  it('OSS Telegram persists via updateChannelConfig (telegram_allowed_chat_id)', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      bluebubbles_imessage_address: '',
      imessage_backend: null,
    });
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('123456789');
    await user.type(input, '987654321');
    await user.click(screen.getByRole('button', { name: 'Open Telegram' }));
    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({
        telegram_allowed_chat_id: '987654321',
      });
    });
  });

  it('Telegram flow rejects non-numeric IDs with an inline error', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
      bluebubbles_configured: false,
      bluebubbles_allowed_numbers: '',
      bluebubbles_imessage_address: '',
      imessage_backend: null,
    });
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('123456789');
    await user.type(input, 'not-a-number');
    await user.click(screen.getByRole('button', { name: 'Open Telegram' }));
    await waitFor(() => {
      expect(screen.getByText(/Use your numeric Telegram user ID/)).toBeInTheDocument();
    });
    expect(mockUpdateChannelConfig).not.toHaveBeenCalled();
  });
});
