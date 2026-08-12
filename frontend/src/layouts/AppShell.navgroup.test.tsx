import { createElement } from 'react';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import AppShell from '@/layouts/AppShell';
import type { NavExtensionItem } from '@/extensions';

// Nav-group behavior is driven entirely by what an extension returns from
// getExtraNavItems, so this file mocks that one export and leaves the rest of
// the extension surface at its OSS defaults.

const CHILD_ITEMS: NavExtensionItem[] = [
  { to: '/app/admin', label: 'Overview', icon: StubIcon },
  { to: '/app/admin/users', label: 'Users', icon: StubIcon },
  { to: '/app/admin/config', label: 'Config', icon: StubIcon },
];

function StubIcon() {
  return createElement('svg', { className: 'w-5 h-5 shrink-0' });
}

const groupedItem: NavExtensionItem = {
  to: '/app/admin',
  label: 'Admin',
  icon: StubIcon,
  children: CHILD_ITEMS,
};

vi.mock('@/extensions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/extensions')>();
  return {
    ...actual,
    getExtraNavItems: () => [groupedItem],
  };
});

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    authState: 'ready',
    currentAuthUser: { id: 1, name: 'Test User', role: 'admin' },
    authConfig: { required: false },
    isPremium: true,
    handleLogin: vi.fn(),
    handleLogout: vi.fn(),
  }),
}));

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
  user_id: 'admin@example.com',
  phone: '+15555550123',
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

beforeEach(() => {
  mockApi.getProfile.mockResolvedValue(
    PROFILE_RESPONSE as unknown as Awaited<ReturnType<typeof api.getProfile>>,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AppShell extension nav groups', () => {
  it('collapses the group when the route is outside it', async () => {
    renderWithRouter(<AppShell />, { route: '/app/dashboard' });

    await waitFor(() => {
      expect(screen.getByText('Admin')).toBeInTheDocument();
    });
    expect(screen.queryByText('Users')).not.toBeInTheDocument();
    expect(screen.queryByText('Config')).not.toBeInTheDocument();
  });

  it('expands via the chevron without navigating away', async () => {
    renderWithRouter(<AppShell />, { route: '/app/dashboard' });

    await waitFor(() => {
      expect(screen.getByText('Admin')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Expand Admin' }));

    expect(screen.getByText('Users')).toBeInTheDocument();
    expect(screen.getByText('Config')).toBeInTheDocument();
    // The parent link is still a link, and the chevron flipped to Collapse.
    expect(screen.getByRole('link', { name: /Admin/ })).toHaveAttribute('href', '/app/admin');
    expect(screen.getByRole('button', { name: 'Collapse Admin' })).toBeInTheDocument();
  });

  it('auto-expands when the current route is inside the group', async () => {
    renderWithRouter(<AppShell />, { route: '/app/admin/users' });

    await waitFor(() => {
      expect(screen.getByText('Users')).toBeInTheDocument();
    });
    expect(screen.getByText('Config')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Collapse Admin' })).toBeInTheDocument();
  });

  it('auto-expands on a route deeper than a child (e.g. a user detail page)', async () => {
    renderWithRouter(<AppShell />, { route: '/app/admin/users/abc-123' });

    await waitFor(() => {
      expect(screen.getByText('Users')).toBeInTheDocument();
    });
    // The Users child stays highlighted for its own descendants.
    expect(screen.getByRole('link', { name: /Users/ })).toHaveClass('bg-selected-bg');
  });

  it('does not keep the parent highlighted while a child route is active', async () => {
    renderWithRouter(<AppShell />, { route: '/app/admin/config' });

    await waitFor(() => {
      expect(screen.getByText('Config')).toBeInTheDocument();
    });
    const parent = screen
      .getAllByRole('link')
      .find(a => a.getAttribute('href') === '/app/admin' && a.textContent?.includes('Admin'));
    expect(parent).toBeDefined();
    expect(parent).not.toHaveClass('bg-selected-bg');
  });

  it('moves the highlight onto the parent once the group is collapsed', async () => {
    renderWithRouter(<AppShell />, { route: '/app/admin/config' });

    await waitFor(() => expect(screen.getByText('Config')).toBeInTheDocument());
    const parent = () =>
      screen
        .getAllByRole('link')
        .find(a => a.getAttribute('href') === '/app/admin' && a.textContent?.includes('Admin'))!;
    expect(parent()).not.toHaveClass('bg-selected-bg');

    // Collapsed, the active child is hidden, so nothing would show where the
    // user is unless the parent picks the highlight up.
    await userEvent.setup().click(screen.getByRole('button', { name: 'Collapse Admin' }));
    expect(screen.queryByText('Config')).not.toBeInTheDocument();
    expect(parent()).toHaveClass('bg-selected-bg');
  });

  it('leaves the parent unhighlighted when collapsed outside the group', async () => {
    renderWithRouter(<AppShell />, { route: '/app/dashboard' });

    await waitFor(() => expect(screen.getByText('Admin')).toBeInTheDocument());
    const parent = screen
      .getAllByRole('link')
      .find(a => a.getAttribute('href') === '/app/admin' && a.textContent?.includes('Admin'));
    expect(parent).not.toHaveClass('bg-selected-bg');
  });

  it('still lets the user collapse a group they are inside', async () => {
    renderWithRouter(<AppShell />, { route: '/app/admin/users' });

    await waitFor(() => {
      expect(screen.getByText('Users')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Collapse Admin' }));

    expect(screen.queryByText('Config')).not.toBeInTheDocument();
  });
});
