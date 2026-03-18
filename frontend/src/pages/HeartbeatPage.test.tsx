import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import HeartbeatPage from './HeartbeatPage';

// Mock outlet context (profile + reloadProfile)
const mockProfile = {
  id: 'u1',
  user_id: 'u1',
  phone: '',
  timezone: 'America/New_York',
  soul_text: '',
  user_text: '',
  heartbeat_text: 'Some freeform notes',
  preferred_channel: 'telegram',
  channel_identifier: '',
  heartbeat_opt_in: true,
  heartbeat_frequency: 'daily',
  onboarding_complete: true,
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
};

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useOutletContext: () => ({
      profile: mockProfile,
      reloadProfile: vi.fn(),
      isPremium: false,
      isAdmin: false,
    }),
  };
});

vi.mock('@/api', () => ({
  default: {
    listHeartbeatItems: vi.fn(),
    createHeartbeatItem: vi.fn(),
    updateHeartbeatItem: vi.fn(),
    deleteHeartbeatItem: vi.fn(),
    getProfile: vi.fn(),
    updateProfile: vi.fn(),
  },
}));

import api from '@/api';
const mockApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.getProfile.mockResolvedValue(mockProfile as never);
});

describe('HeartbeatPage', () => {
  it('calls listHeartbeatItems API on mount', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([]);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(mockApi.listHeartbeatItems).toHaveBeenCalledOnce();
    });
  });

  it('displays structured heartbeat items from API', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([
      {
        id: 1,
        description: 'Follow up with new leads',
        schedule: 'daily',
        status: 'active',
        created_at: '2025-01-01T00:00:00Z',
      },
      {
        id: 2,
        description: 'Check on active job sites',
        schedule: 'weekly',
        status: 'active',
        created_at: '2025-01-02T00:00:00Z',
      },
    ]);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByText('Follow up with new leads')).toBeInTheDocument();
    });
    expect(screen.getByText('Check on active job sites')).toBeInTheDocument();
    expect(screen.getByText('daily')).toBeInTheDocument();
    expect(screen.getByText('weekly')).toBeInTheDocument();
  });

  it('shows empty state when no items exist', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([]);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(
        screen.getByText('No items yet. Add one below, or ask your assistant to create them.'),
      ).toBeInTheDocument();
    });
  });

  it('creates a new heartbeat item via API', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([]);
    mockApi.createHeartbeatItem.mockResolvedValue({
      id: 3,
      description: 'Send invoice to client',
      schedule: 'weekly',
      status: 'active',
      created_at: '2025-01-03T00:00:00Z',
    });

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByPlaceholderText('New item description')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText('New item description'), {
      target: { value: 'Send invoice to client' },
    });
    fireEvent.change(
      screen.getByPlaceholderText('Schedule (optional, e.g. daily, weekly)'),
      { target: { value: 'weekly' } },
    );
    fireEvent.click(screen.getByRole('button', { name: /add/i }));

    await waitFor(() => {
      expect(mockApi.createHeartbeatItem).toHaveBeenCalledWith({
        description: 'Send invoice to client',
        schedule: 'weekly',
      });
    });
  });

  it('deletes a heartbeat item via API', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([
      {
        id: 1,
        description: 'Follow up with new leads',
        schedule: 'daily',
        status: 'active',
        created_at: '2025-01-01T00:00:00Z',
      },
    ]);
    mockApi.deleteHeartbeatItem.mockResolvedValue(undefined);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByText('Follow up with new leads')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /delete/i }));

    await waitFor(() => {
      expect(mockApi.deleteHeartbeatItem).toHaveBeenCalledWith(1);
    });
  });

  it('also displays freeform notes textarea', async () => {
    mockApi.listHeartbeatItems.mockResolvedValue([]);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByText('Notes')).toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText(
      'Additional notes for your assistant (markdown supported)',
    );
    expect(textarea).toBeInTheDocument();
    expect(textarea).toHaveValue('Some freeform notes');
  });
});
