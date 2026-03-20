import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import GetStartedPage from './GetStartedPage';

const mockNavigate = vi.fn();
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

vi.mock('@/api', () => ({
  default: {
    updateProfile: vi.fn().mockResolvedValue({ onboarding_complete: true }),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GetStartedPage', () => {
  it('renders the get started heading and all step cards', () => {
    renderWithRouter(<GetStartedPage />);

    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(screen.getByText('Set up Telegram')).toBeInTheDocument();
    expect(screen.getByText('Tell it about you')).toBeInTheDocument();
    expect(screen.getByText('Customize its personality')).toBeInTheDocument();
    expect(screen.getByText('Start chatting')).toBeInTheDocument();
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
});
