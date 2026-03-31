import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import AppShell from '@/layouts/AppShell';

// Mock auth context
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

// Mock the api module
vi.mock('@/api', () => ({
  default: {
    getProfile: vi.fn(),
  },
}));

import api from '@/api';
const mockApi = vi.mocked(api);

const PROFILE_RESPONSE = {
  id: '1',
  user_id: 'local@clawbolt.local',
  phone: '555-0100',
  timezone: 'America/Los_Angeles',
  soul_text: '',
  user_text: '',
  heartbeat_text: '',
  preferred_channel: 'telegram',
  channel_identifier: '',
  heartbeat_opt_in: true,
  heartbeat_frequency: 'daily',
  onboarding_complete: true,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

function setupMocks(
  profile: unknown = PROFILE_RESPONSE,
) {
  mockApi.getProfile.mockResolvedValue(profile as ReturnType<typeof api.getProfile> extends Promise<infer T> ? T : never);
}

beforeEach(() => {
  setupMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AppShell', () => {
  it('renders navigation links', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument();
    });
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('Heartbeat')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('renders Dashboard first and Chat last in sidebar', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument();
    });

    const nav = document.querySelector('nav');
    const links = nav?.querySelectorAll('a');
    if (links && links.length > 0) {
      expect(links[0]?.textContent).toContain('Dashboard');
      expect(links[links.length - 1]?.textContent).toContain('Chat');
    }
  });

  it('does not render a Conversations nav link', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });
    expect(screen.queryByText('Conversations')).not.toBeInTheDocument();
  });

  it('main element has min-h-0 for iOS Safari flex layout fix', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });

    const main = document.querySelector('main');
    expect(main).toHaveClass('min-h-0');
  });

  it('shows error state when profile fails to load', async () => {
    mockApi.getProfile.mockRejectedValue(new Error('Network error'));

    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText(/unable to load your profile/i)).toBeInTheDocument();
    });
  });

  it('renders default sidebar footer when renderSidebarFooter stub returns null', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Get Started')).toBeInTheDocument();
    });
    expect(screen.getByText('Report issue')).toBeInTheDocument();
    expect(screen.getByText('Feature request')).toBeInTheDocument();
  });
});
