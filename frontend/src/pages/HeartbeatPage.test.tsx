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
  heartbeat_text: '- [ ] Follow up with new leads',
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
  it('renders heartbeat content as markdown by default', async () => {
    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByRole('listitem')).toHaveTextContent('Follow up with new leads');
    });

    // Should show Edit button, not Save
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
  });

  it('switches to textarea on Edit click and saves on Save click', async () => {
    mockApi.updateProfile.mockResolvedValue(mockProfile as never);

    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
    });

    // Click Edit to enter edit mode
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));

    // Should now show textarea with the raw markdown
    const textarea = screen.getByRole('textbox');
    expect(textarea).toHaveValue('- [ ] Follow up with new leads');

    // Modify and save
    fireEvent.change(textarea, { target: { value: 'Updated notes' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(mockApi.updateProfile).toHaveBeenCalled();
      expect(mockApi.updateProfile.mock.calls[0]?.[0]).toEqual({
        heartbeat_text: 'Updated notes',
      });
    });
  });

  it('reverts changes on Cancel', async () => {
    renderWithRouter(<HeartbeatPage />, { route: '/app/heartbeat' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /edit/i }));

    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'Changed text' } });

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

    // Should be back in view mode showing original content
    await waitFor(() => {
      expect(screen.getByRole('listitem')).toHaveTextContent('Follow up with new leads');
    });
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
