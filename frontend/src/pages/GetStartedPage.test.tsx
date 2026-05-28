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
    getDataSharingConsent: vi.fn().mockResolvedValue({
      data_sharing_consent: false,
      data_sharing_consent_at: null,
    }),
    updateDataSharingConsent: vi.fn().mockResolvedValue({
      data_sharing_consent: true,
      data_sharing_consent_at: '2026-05-01T00:00:00+00:00',
    }),
  },
}));

// Default channel config used by every test unless explicitly overridden.
// Encapsulated so beforeEach can re-pin it: ``mockResolvedValue`` overrides
// inside individual tests are otherwise sticky and bleed into the next test.
const defaultChannelConfig = {
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: true,
  linq_from_number: '+15559876543',
  linq_allowed_numbers: '',
  linq_preferred_service: 'iMessage',
  bluebubbles_configured: false,
  bluebubbles_allowed_numbers: '',
  imessage_backend: 'linq',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockIsPremium = false;
  mockGetChannelConfig.mockResolvedValue(defaultChannelConfig);
  mockGetChannelRoutes.mockResolvedValue({ routes: [] });
  mockToggleChannelRoute.mockResolvedValue({
    channel: 'linq', channel_identifier: '', enabled: true, created_at: '',
  });
});

describe('GetStartedPage', () => {
  it('renders the get started heading and intro copy', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(
      screen.getByText(/Clawbolt is your AI assistant for the trades/),
    ).toBeInTheDocument();
  });

  it('does not show the legacy four-step wizard copy', async () => {
    // The collapsed one-card layout drops the Step 1-4 framing. If any of
    // these reappear, the wizard regressed.
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
    });
    expect(screen.queryByText('Choose your messaging channel')).not.toBeInTheDocument();
    expect(screen.queryByText('Send a message')).not.toBeInTheDocument();
    expect(screen.queryByText("You're off to the races")).not.toBeInTheDocument();
    // No "None" radio either; the bottom-of-page "Use web chat instead" link
    // covers users who want to skip messaging setup.
    expect(screen.queryByDisplayValue('none')).not.toBeInTheDocument();
  });

  it('auto-renders the sole configured channel form without a chooser', async () => {
    // Default fixture has only Linq configured. With one channel visible,
    // the toggle is skipped and the form is rendered directly.
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
    });
    expect(screen.queryByRole('tab', { name: 'iMessage' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Telegram' })).not.toBeInTheDocument();
  });

  it('shows a channel toggle when iMessage and Telegram are both configured', async () => {
    // ``mockResolvedValue`` (sticky) instead of ``mockResolvedValueOnce`` so
    // the auto-select mutation's ``invalidateQueries(channels)`` refetch sees
    // the same multi-channel config. ``beforeEach`` resets it next test.
    mockGetChannelConfig.mockResolvedValue({
      ...defaultChannelConfig,
      telegram_bot_token_set: true,
    });

    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'iMessage' })).toBeInTheDocument();
    });
    expect(screen.getByRole('tab', { name: 'Telegram' })).toBeInTheDocument();
  });

  it('shows the dashboard dismiss button once a channel is auto-selected', async () => {
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByText('Got it, take me to the dashboard')).toBeInTheDocument();
    });
  });

  it('renders the OSS linq config form when only Linq is configured', async () => {
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
    });
  });

  it('swaps to the telegram form when the user picks Telegram in the toggle', async () => {
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

    const telegramTab = await screen.findByRole('tab', { name: 'Telegram' });
    await user.click(telegramTab);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
    });
  });

  it('saves linq config via the shared form (updateChannelConfig)', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const input = await screen.findByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({ linq_allowed_numbers: '+15551234567' });
    });
  });

  it('does NOT enable the route on mount; defers to form save', async () => {
    // Auto-pick only sets display state. The channel route stays disabled
    // until the user commits via Save, so a user who lands on the page
    // and bails via "Use web chat instead" never silently flips a route.
    renderWithRouter(<GetStartedPage />);

    await screen.findByPlaceholderText('e.g. +15551234567');
    // Settle any pending microtasks just to be sure.
    await waitFor(() => {
      expect(mockGetChannelConfig).toHaveBeenCalled();
    });
    expect(mockToggleChannelRoute).not.toHaveBeenCalled();
  });

  it('enables the channel route when the user saves the form', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const input = await screen.findByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('linq', true);
    });
  });

  it('disables the previously enabled channel when the user saves a different one', async () => {
    // Land with Telegram already enabled; switch to iMessage via the tab
    // toggle; save the iMessage form. The save should both disable Telegram
    // and enable Linq, leaving exactly one active messaging route.
    mockGetChannelConfig.mockResolvedValue({
      ...defaultChannelConfig,
      telegram_bot_token_set: true,
      telegram_allowed_chat_id: '123',
    });
    mockGetChannelRoutes.mockResolvedValue({
      routes: [{ channel: 'telegram', channel_identifier: '123', enabled: true, created_at: '' }],
    });

    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const imessageTab = await screen.findByRole('tab', { name: 'iMessage' });
    await user.click(imessageTab);

    const input = await screen.findByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockToggleChannelRoute).toHaveBeenCalledWith('telegram', false);
    });
    expect(mockToggleChannelRoute).toHaveBeenCalledWith('linq', true);
  });

  it('does not re-toggle when saving the already-active channel', async () => {
    // ``pre-populates the toggle selection`` confirms the form pre-fills
    // for the active route. Re-saving that same channel should not fire a
    // redundant enable on a route that is already enabled.
    mockGetChannelRoutes.mockResolvedValue({
      routes: [{ channel: 'linq', channel_identifier: '+15551234567', enabled: true, created_at: '' }],
    });

    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const input = await screen.findByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalled();
    });
    expect(mockToggleChannelRoute).not.toHaveBeenCalled();
  });

  it('does not fire any route mutation when the user just switches tabs', async () => {
    // Clicking between tabs is a display-only action; nothing hits the
    // backend until the user commits via Save.
    mockGetChannelConfig.mockResolvedValue({
      ...defaultChannelConfig,
      telegram_bot_token_set: true,
    });

    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const telegramTab = await screen.findByRole('tab', { name: 'Telegram' });
    await user.click(telegramTab);
    const imessageTab = screen.getByRole('tab', { name: 'iMessage' });
    await user.click(imessageTab);

    expect(mockToggleChannelRoute).not.toHaveBeenCalled();
  });

  it('shows QR code and from-number when linq is configured', async () => {
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      // From-number appears in both the config form and the QR card
      const matches = screen.getAllByText('+15559876543');
      expect(matches.length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText(/Send an iMessage to this address to get started/)).toBeInTheDocument();
  });

  it('shows the empty-state card when no channels are configured', async () => {
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
    // No channel form rendered.
    expect(screen.queryByPlaceholderText('e.g. +15551234567')).not.toBeInTheDocument();
    // Dismiss button stays on the chat-oriented copy because no channel is selected.
    expect(screen.getByText('Got it, take me to chat')).toBeInTheDocument();
  });

  it('keeps the data-sharing consent card visible on desktop', async () => {
    renderWithRouter(<GetStartedPage />);

    // ``findByRole`` waits for the consent query to resolve. ``getByText``
    // on "Help improve Clawbolt" alone passes during the loading state and
    // races the checkbox check.
    expect(
      await screen.findByRole('checkbox', { name: /share my chat history/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Help improve Clawbolt')).toBeInTheDocument();
  });

  it('renders a "Use web chat instead" link that navigates to chat', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const link = await screen.findByRole('button', { name: 'Use web chat instead' });
    await user.click(link);

    expect(mockNavigate).toHaveBeenCalledWith('/app/chat');
  });

  it('pre-populates the toggle selection from an active channel route', async () => {
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
      expect(
        screen.getByRole('tab', { name: 'Telegram', selected: true }),
      ).toBeInTheDocument();
    });
    expect(screen.getByPlaceholderText('e.g. 123456789')).toBeInTheDocument();
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

  it('shows the data-sharing consent checkbox on mobile onboarding', async () => {
    renderWithRouter(<GetStartedPage />);
    await waitFor(() => {
      expect(screen.getByText("Hey, I'm Clawbolt")).toBeInTheDocument();
    });
    // Heading + interactive checkbox both have to be present, otherwise we've
    // dropped the share-usage opt-in from the mobile flow.
    expect(screen.getByText('Help improve Clawbolt')).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: /share my chat history/i }),
    ).toBeInTheDocument();
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
