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
      profile: { onboarding_complete: false },
      reloadProfile: mockReloadProfile,
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
    isPremium: false,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

vi.mock('@/lib/api-client', () => ({
  getAccessToken: () => 'test-token',
  default: {
    GET: vi.fn(),
    PUT: vi.fn(),
  },
}));

const mockUpdateProfile = vi.fn().mockResolvedValue({ onboarding_complete: true });
const mockUpdateChannelConfig = vi.fn().mockResolvedValue({
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: true,
  linq_from_number: '+15559876543',
  linq_allowed_numbers: '+15551234567',
  linq_preferred_service: 'iMessage',
});
const mockGetChannelConfig = vi.fn().mockResolvedValue({
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: true,
  linq_from_number: '+15559876543',
  linq_allowed_numbers: '',
  linq_preferred_service: 'iMessage',
});

vi.mock('@/api', () => ({
  default: {
    updateProfile: (...args: unknown[]) => mockUpdateProfile(...args),
    getProfile: vi.fn().mockResolvedValue({ onboarding_complete: false }),
    getChannelConfig: (...args: unknown[]) => mockGetChannelConfig(...args),
    updateChannelConfig: (...args: unknown[]) => mockUpdateChannelConfig(...args),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GetStartedPage', () => {
  it('renders the get started heading and phone number step', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(screen.getByText('Enter your phone number')).toBeInTheDocument();
    expect(screen.getByText('Send a message')).toBeInTheDocument();
    expect(screen.getByText("You're off to the races")).toBeInTheDocument();
  });

  it('renders a phone number input field', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByPlaceholderText('e.g. +15551234567')).toBeInTheDocument();
  });

  it('renders the dismiss button', () => {
    renderWithRouter(<GetStartedPage />);
    expect(screen.getByText('Got it, take me to chat')).toBeInTheDocument();
  });

  it('shows save button that is disabled when phone input is empty', () => {
    renderWithRouter(<GetStartedPage />);

    const saveButton = screen.getByRole('button', { name: 'Save' });
    expect(saveButton).toBeDisabled();
  });

  it('enables save button when phone number is entered', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const input = screen.getByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');

    const saveButton = screen.getByRole('button', { name: 'Save' });
    expect(saveButton).not.toBeDisabled();
  });

  it('saves phone number and shows edit button on success', async () => {
    renderWithRouter(<GetStartedPage />);
    const user = userEvent.setup();

    const input = screen.getByPlaceholderText('e.g. +15551234567');
    await user.type(input, '+15551234567');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockUpdateChannelConfig).toHaveBeenCalledWith({ linq_allowed_numbers: '+15551234567' });
    });
  });

  it('shows QR code and from-number when linq is configured', async () => {
    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByText('+15559876543')).toBeInTheDocument();
    });
    expect(screen.getByText(/Text this number from your phone/)).toBeInTheDocument();
  });

  it('shows fallback messaging when linq is not configured', async () => {
    mockGetChannelConfig.mockResolvedValueOnce({
      telegram_bot_token_set: false,
      telegram_allowed_chat_id: '',
      linq_api_token_set: false,
      linq_from_number: '',
      linq_allowed_numbers: '',
      linq_preferred_service: 'iMessage',
    });

    renderWithRouter(<GetStartedPage />);

    await waitFor(() => {
      expect(screen.getByText(/Text messaging is not configured yet/)).toBeInTheDocument();
    });
  });
});
