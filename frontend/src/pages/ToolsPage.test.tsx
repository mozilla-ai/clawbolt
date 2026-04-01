import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import ToolsPage from './ToolsPage';

const mockGetToolConfig = vi.fn();
const mockGetOAuthStatus = vi.fn();
const mockGetCalendarList = vi.fn();
const mockGetCalendarConfig = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getToolConfig: (...args: unknown[]) => mockGetToolConfig(...args),
    updateToolConfig: vi.fn().mockResolvedValue({}),
    getOAuthStatus: (...args: unknown[]) => mockGetOAuthStatus(...args),
    getOAuthAuthorizeUrl: vi.fn().mockResolvedValue({ url: 'https://example.com' }),
    disconnectOAuth: vi.fn().mockResolvedValue({}),
    getCalendarList: (...args: unknown[]) => mockGetCalendarList(...args),
    getCalendarConfig: (...args: unknown[]) => mockGetCalendarConfig(...args),
    updateCalendarConfig: vi.fn().mockResolvedValue({}),
  },
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User' },
    authConfig: { required: true, method: 'oidc' },
    isPremium: false,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useOutletContext: () => ({ profile: {}, reloadProfile: vi.fn() }),
  };
});

function setupMocks(overrides?: {
  tools?: unknown;
  oauth?: unknown;
  calendarList?: unknown;
  calendarConfig?: unknown;
}) {
  mockGetToolConfig.mockResolvedValue(
    overrides?.tools ?? {
      tools: [
        { name: 'workspace', description: 'Workspace tools', category: 'core', enabled: true, domain_group: '', domain_group_order: 0 },
        { name: 'calendar', description: 'Google Calendar integration', category: 'domain', enabled: true, domain_group: '', domain_group_order: 0 },
      ],
    },
  );
  mockGetOAuthStatus.mockResolvedValue(
    overrides?.oauth ?? {
      integrations: [{ integration: 'google_calendar', connected: true, configured: true }],
    },
  );
  mockGetCalendarList.mockResolvedValue(
    overrides?.calendarList ?? { calendars: [] },
  );
  mockGetCalendarConfig.mockResolvedValue(
    overrides?.calendarConfig ?? { calendars: [] },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ToolsPage', () => {
  it('shows "Not configured" with dimmed card when integration is not configured', async () => {
    setupMocks({
      oauth: {
        integrations: [{ integration: 'google_calendar', connected: false, configured: false }],
      },
    });
    renderWithRouter(<ToolsPage />);

    await waitFor(() => {
      expect(screen.getByText('Google Calendar')).toBeInTheDocument();
    });

    // Should show "Not configured" status text
    expect(screen.getByText('Not configured')).toBeInTheDocument();

    // Should NOT show connect/disconnect buttons
    expect(screen.queryByText('Connect')).not.toBeInTheDocument();
    expect(screen.queryByText('Disconnect')).not.toBeInTheDocument();

    // Should NOT show the enable/disable toggle
    expect(screen.queryByLabelText('Toggle Google Calendar')).not.toBeInTheDocument();

    // Card should have opacity-50 class for visual dimming
    const cardElement = screen.getByText('Google Calendar').closest('.opacity-50');
    expect(cardElement).not.toBeNull();
  });

  it('shows "Connected" with full card when integration is configured and connected', async () => {
    setupMocks();
    renderWithRouter(<ToolsPage />);

    await waitFor(() => {
      expect(screen.getByText('Google Calendar')).toBeInTheDocument();
    });

    expect(screen.getByText('Connected')).toBeInTheDocument();
    expect(screen.queryByText('Not configured')).not.toBeInTheDocument();

    // Card should NOT have opacity-50
    const cardElement = screen.getByText('Google Calendar').closest('.opacity-50');
    expect(cardElement).toBeNull();
  });

  it('shows "Not connected" with Connect button when configured but not connected', async () => {
    setupMocks({
      oauth: {
        integrations: [{ integration: 'google_calendar', connected: false, configured: true }],
      },
    });
    renderWithRouter(<ToolsPage />);

    await waitFor(() => {
      expect(screen.getByText('Google Calendar')).toBeInTheDocument();
    });

    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.getByText('Connect')).toBeInTheDocument();
    expect(screen.queryByText('Not configured')).not.toBeInTheDocument();

    // Card should NOT have opacity-50
    const cardElement = screen.getByText('Google Calendar').closest('.opacity-50');
    expect(cardElement).toBeNull();
  });
});
