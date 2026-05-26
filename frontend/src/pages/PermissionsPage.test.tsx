import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import PermissionsPage from './PermissionsPage';

const mockGetToolConfig = vi.fn();
const mockGetPermissions = vi.fn();
const mockGetOAuthStatus = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getToolConfig: (...args: unknown[]) => mockGetToolConfig(...args),
    getPermissions: (...args: unknown[]) => mockGetPermissions(...args),
    updatePermissions: vi.fn().mockResolvedValue({}),
    getOAuthStatus: (...args: unknown[]) => mockGetOAuthStatus(...args),
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

function tool(overrides: Record<string, unknown>) {
  return {
    name: 'demo',
    description: '',
    category: 'core',
    domain_group: '',
    domain_group_order: 0,
    enabled: true,
    configured: true,
    auth_message: '',
    oauth_name: '',
    always_enabled: false,
    sub_tools: [{ name: 'demo_action', permission_level: 'ask', hidden_in_permissions: false }],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetPermissions.mockResolvedValue({ content: '{}' });
});

describe('PermissionsPage', () => {
  it('hides OAuth integrations that are not connected', async () => {
    mockGetToolConfig.mockResolvedValue({
      tools: [
        tool({ name: 'workspace', category: 'core' }),
        tool({
          name: 'calendar',
          category: 'domain',
          oauth_name: 'google_calendar',
          sub_tools: [
            { name: 'calendar_list_events', permission_level: 'ask', hidden_in_permissions: false },
          ],
        }),
      ],
    });
    mockGetOAuthStatus.mockResolvedValue({
      integrations: [{ integration: 'google_calendar', connected: false, configured: true }],
    });

    renderWithRouter(<PermissionsPage />);

    await waitFor(() => {
      expect(screen.getByText('Workspace')).toBeInTheDocument();
    });
    expect(screen.queryByText('Google Calendar')).not.toBeInTheDocument();
  });

  it('shows OAuth integrations once they are connected', async () => {
    mockGetToolConfig.mockResolvedValue({
      tools: [
        tool({
          name: 'calendar',
          category: 'domain',
          oauth_name: 'google_calendar',
          sub_tools: [
            { name: 'calendar_list_events', permission_level: 'ask', hidden_in_permissions: false },
          ],
        }),
      ],
    });
    mockGetOAuthStatus.mockResolvedValue({
      integrations: [{ integration: 'google_calendar', connected: true, configured: true }],
    });

    renderWithRouter(<PermissionsPage />);

    await waitFor(() => {
      expect(screen.getByText('Google Calendar')).toBeInTheDocument();
    });
  });

  it('hides non-OAuth tools whose backend reports configured=false', async () => {
    mockGetToolConfig.mockResolvedValue({
      tools: [
        tool({
          name: 'supplier_pricing',
          category: 'domain',
          configured: false,
          sub_tools: [
            { name: 'search_supplier', permission_level: 'ask', hidden_in_permissions: false },
          ],
        }),
      ],
    });
    mockGetOAuthStatus.mockResolvedValue({ integrations: [] });

    renderWithRouter(<PermissionsPage />);

    await waitFor(() => {
      expect(mockGetToolConfig).toHaveBeenCalled();
    });
    expect(screen.queryByText('Pricing Tools')).not.toBeInTheDocument();
  });

  it('hides OAuth-backed core tools (e.g. Google Drive) when not connected', async () => {
    mockGetToolConfig.mockResolvedValue({
      tools: [
        tool({
          name: 'file',
          category: 'core',
          oauth_name: 'google_drive',
          always_enabled: true,
          sub_tools: [
            { name: 'save_file', permission_level: 'ask', hidden_in_permissions: false },
          ],
        }),
      ],
    });
    mockGetOAuthStatus.mockResolvedValue({
      integrations: [{ integration: 'google_drive', connected: false, configured: true }],
    });

    renderWithRouter(<PermissionsPage />);

    await waitFor(() => {
      expect(mockGetToolConfig).toHaveBeenCalled();
    });
    expect(screen.queryByText('Google Drive')).not.toBeInTheDocument();
  });
});
