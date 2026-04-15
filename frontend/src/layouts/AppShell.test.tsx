import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Routes, Route } from 'react-router-dom';
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
    subscribeToActivity: vi.fn().mockReturnValue(new AbortController()),
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

  // Regression: sending a chat message and then switching tabs used to abort the
  // activity SSE subscription because it lived inside ChatPage. The subscription
  // now lives in ChatActivityProvider at AppShell level, so navigation between
  // app routes must not tear it down or spawn extra subscriptions.
  it('keeps the activity subscription alive across route changes', async () => {
    const controller = new AbortController();
    const abortSpy = vi.spyOn(controller, 'abort');
    mockApi.subscribeToActivity.mockClear();
    mockApi.subscribeToActivity.mockReturnValue(controller);

    renderWithRouter(
      <Routes>
        <Route path="/app" element={<AppShell />}>
          <Route path="chat" element={<div>Chat route</div>} />
          <Route path="permissions" element={<div>Permissions route</div>} />
        </Route>
      </Routes>,
      { route: '/app/chat' },
    );

    await waitFor(() => {
      expect(screen.getByText('Chat route')).toBeInTheDocument();
    });
    expect(mockApi.subscribeToActivity).toHaveBeenCalledTimes(1);

    const user = userEvent.setup();
    await user.click(screen.getByText('Permissions'));

    await waitFor(() => {
      expect(screen.getByText('Permissions route')).toBeInTheDocument();
    });

    expect(mockApi.subscribeToActivity).toHaveBeenCalledTimes(1);
    expect(abortSpy).not.toHaveBeenCalled();
  });

  it('exposes activity state to descendants and updates on events', async () => {
    let capturedOnEvent:
      | ((event: { type: string; tool_name?: string }) => void)
      | null = null;
    mockApi.subscribeToActivity.mockImplementation((onEvent) => {
      capturedOnEvent = onEvent;
      return new AbortController();
    });

    const { useChatActivity } = await import('@/contexts/ChatActivityContext');
    function ActivityProbe() {
      const { agentBusy, activityTool } = useChatActivity();
      return (
        <div>
          <span data-testid="busy">{agentBusy ? 'yes' : 'no'}</span>
          <span data-testid="tool">{activityTool ?? 'none'}</span>
        </div>
      );
    }

    renderWithRouter(
      <Routes>
        <Route path="/app" element={<AppShell />}>
          <Route path="chat" element={<ActivityProbe />} />
        </Route>
      </Routes>,
      { route: '/app/chat' },
    );

    await waitFor(() => {
      expect(screen.getByTestId('busy')).toHaveTextContent('no');
    });

    expect(capturedOnEvent).not.toBeNull();
    act(() => {
      capturedOnEvent!({ type: 'tool_call', tool_name: 'search_web' });
    });

    await waitFor(() => {
      expect(screen.getByTestId('busy')).toHaveTextContent('yes');
    });
    expect(screen.getByTestId('tool')).toHaveTextContent('search_web');

    act(() => {
      capturedOnEvent!({ type: 'done' });
    });

    await waitFor(() => {
      expect(screen.getByTestId('busy')).toHaveTextContent('no');
    });
    expect(screen.getByTestId('tool')).toHaveTextContent('none');
  });
});
