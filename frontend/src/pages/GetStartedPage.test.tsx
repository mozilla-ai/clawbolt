import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import GetStartedPage from './GetStartedPage';

const mockNavigate = vi.fn();
let mockIsPremium = false;

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useOutletContext: () => ({
      profile: { onboarding_complete: false },
      reloadProfile: vi.fn(),
      isPremium: false,
      isAdmin: false,
    }),
  };
});

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

vi.mock('@/lib/api-client', () => ({
  getAccessToken: () => 'test-token',
}));

vi.mock('@/api', () => ({
  default: {
    updateProfile: vi.fn().mockResolvedValue({ onboarding_complete: true }),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockIsPremium = false;
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
});

describe('GetStartedPage', () => {
  it('renders the get started heading and all step cards', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(screen.getByText('Set up Telegram')).toBeInTheDocument();
    expect(screen.getByText('Start chatting')).toBeInTheDocument();
    expect(screen.queryByText('Tell it about you')).not.toBeInTheDocument();
    expect(screen.queryByText('Customize its personality')).not.toBeInTheDocument();
  });

  it('navigates to channels page when configure channels is clicked', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    await user.click(screen.getByText('Configure Channels'));
    expect(mockNavigate).toHaveBeenCalledWith('/app/channels');
  });

  it('renders the dismiss button', () => {
    renderWithRouter(<GetStartedPage />);
    expect(screen.getByText('Got it, take me to chat')).toBeInTheDocument();
  });

  it('shows bot username in step 1 when premium and bot-info is available', async () => {
    mockIsPremium = true;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ bot_username: 'trades_bot' }),
    }));

    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByText(/Message @trades_bot on Telegram/)).toBeInTheDocument();
    });
  });
});
