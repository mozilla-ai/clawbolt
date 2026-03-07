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

// Mock fetch for profile
beforeEach(() => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({
      id: 1,
      user_id: 'local@clawbolt.local',
      name: 'Test Contractor',
      phone: '555-0100',
      trade: 'Electrician',
      location: 'Portland, OR',
      hourly_rate: 75,
      business_hours: 'Mon-Fri 8-5',
      timezone: 'America/Los_Angeles',
      assistant_name: 'Claw',
      soul_text: '',
      preferred_channel: 'telegram',
      channel_identifier: '',
      heartbeat_opt_in: true,
      heartbeat_frequency: 'daily',
      onboarding_complete: true,
      is_active: true,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AppShell', () => {
  it('renders navigation links', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Overview')).toBeInTheDocument();
    });
    expect(screen.getByText('Conversations')).toBeInTheDocument();
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('Checklist')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('displays contractor name when loaded', async () => {
    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText('Test Contractor')).toBeInTheDocument();
    });
  });

  it('shows error state when profile fails to load', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network error'));

    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText(/unable to load your profile/i)).toBeInTheDocument();
    });
  });
});
