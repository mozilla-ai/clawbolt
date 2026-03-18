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
  it('renders the heartbeat textarea with profile.heartbeat_text value', async () => {
    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText(
          'Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads',
        ),
      ).toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText(
      'Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads',
    );
    expect(textarea).toHaveValue('Some freeform notes');
  });

  it('saves heartbeat_text via updateProfile on button click', async () => {
    mockApi.updateProfile.mockResolvedValue(mockProfile as never);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText(
          'Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads',
        ),
      ).toBeInTheDocument();
    });

    fireEvent.change(
      screen.getByPlaceholderText(
        'Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads',
      ),
      { target: { value: 'Updated notes' } },
    );
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(mockApi.updateProfile).toHaveBeenCalled();
      expect(mockApi.updateProfile.mock.calls[0][0]).toEqual({
        heartbeat_text: 'Updated notes',
      });
    });
  });
});
