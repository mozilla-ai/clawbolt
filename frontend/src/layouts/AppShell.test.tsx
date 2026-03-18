import { screen, waitFor, within } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import AppShell, { formatRelativeTime } from '@/layouts/AppShell';

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
    listSessions: vi.fn(),
    getMemory: vi.fn(),
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

const SESSIONS_RESPONSE = {
  sessions: [
    {
      id: 'session-1',
      start_time: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
      message_count: 3,
      last_message_preview: 'Fix the kitchen faucet leak',
      channel: 'web',
    },
    {
      id: 'session-2',
      start_time: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
      message_count: 7,
      last_message_preview: 'Quote for bathroom remodel',
      channel: 'telegram',
    },
  ],
  total: 2,
  offset: 0,
  limit: 10,
};

function setupMocks(
  profile: unknown = PROFILE_RESPONSE,
  sessions: unknown = SESSIONS_RESPONSE,
) {
  mockApi.getProfile.mockResolvedValue(profile as ReturnType<typeof api.getProfile> extends Promise<infer T> ? T : never);
  mockApi.listSessions.mockResolvedValue(sessions as ReturnType<typeof api.listSessions> extends Promise<infer T> ? T : never);
  mockApi.getMemory.mockResolvedValue({ content: '' });
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
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });
    expect(screen.getByText('Conversations')).toBeInTheDocument();
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('Heartbeat')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('shows error state when profile fails to load', async () => {
    mockApi.getProfile.mockRejectedValue(new Error('Network error'));

    renderWithRouter(<AppShell />, { route: '/app' });

    await waitFor(() => {
      expect(screen.getByText(/unable to load your profile/i)).toBeInTheDocument();
    });
  });
});

describe('RecentConversations', () => {
  it('renders recent conversation entries', async () => {
    renderWithRouter(<AppShell />, { route: '/app/chat' });

    await waitFor(() => {
      expect(screen.getByText('Fix the kitchen faucet leak')).toBeInTheDocument();
    });
    expect(screen.getByText('Quote for bathroom remodel')).toBeInTheDocument();
  });

  it('shows "Recent" heading and "View all" link', async () => {
    renderWithRouter(<AppShell />, { route: '/app/chat' });

    await waitFor(() => {
      expect(screen.getByText('Recent')).toBeInTheDocument();
    });
    expect(screen.getByText('View all')).toBeInTheDocument();

    const viewAllLink = screen.getByText('View all').closest('a');
    expect(viewAllLink).toHaveAttribute('href', '/app/conversations');
  });

  it('links each session to the chat page with session param', async () => {
    renderWithRouter(<AppShell />, { route: '/app/chat' });

    await waitFor(() => {
      expect(screen.getByText('Fix the kitchen faucet leak')).toBeInTheDocument();
    });

    const container = screen.getByTestId('recent-conversations');
    const links = within(container).getAllByRole('link');
    expect(links[0]).toHaveAttribute('href', '/app/chat?session=session-1');
    expect(links[1]).toHaveAttribute('href', '/app/chat?session=session-2');
  });

  it('does not render section when no sessions are returned', async () => {
    setupMocks(PROFILE_RESPONSE, { sessions: [], total: 0, offset: 0, limit: 10 });

    renderWithRouter(<AppShell />, { route: '/app/chat' });

    // Wait for the nav to render (profile loaded)
    await waitFor(() => {
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });

    expect(screen.queryByText('Recent')).not.toBeInTheDocument();
  });

  it('highlights the active session', async () => {
    renderWithRouter(<AppShell />, { route: '/app/chat?session=session-1' });

    await waitFor(() => {
      expect(screen.getByText('Fix the kitchen faucet leak')).toBeInTheDocument();
    });

    const container = screen.getByTestId('recent-conversations');
    const links = within(container).getAllByRole('link');
    expect(links[0]?.className).toContain('bg-selected-bg');
    expect(links[1]?.className).not.toContain('bg-selected-bg');
  });
});

describe('formatRelativeTime', () => {
  it('returns "just now" for recent timestamps', () => {
    const now = new Date().toISOString();
    expect(formatRelativeTime(now)).toBe('just now');
  });

  it('returns minutes ago', () => {
    const fiveMinAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    expect(formatRelativeTime(fiveMinAgo)).toBe('5m ago');
  });

  it('returns hours ago', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    expect(formatRelativeTime(twoHoursAgo)).toBe('2h ago');
  });

  it('returns days ago', () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    expect(formatRelativeTime(threeDaysAgo)).toBe('3d ago');
  });
});
