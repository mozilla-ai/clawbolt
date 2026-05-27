import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import UpdateAvailableBanner from './UpdateAvailableBanner';

type Setter<T> = (v: T | ((prev: T) => T)) => void;

let mockNeedRefresh = false;
const mockSetNeedRefresh = vi.fn<Setter<boolean>>();
const mockUpdateServiceWorker = vi.fn<(reload?: boolean) => Promise<void>>();
const mockUseRegisterSW = vi.fn();

vi.mock('virtual:pwa-register/react', () => ({
  useRegisterSW: (options?: unknown) => mockUseRegisterSW(options),
}));

beforeEach(() => {
  mockNeedRefresh = false;
  mockSetNeedRefresh.mockReset();
  mockUpdateServiceWorker.mockReset();
  mockUpdateServiceWorker.mockResolvedValue(undefined);
  mockUseRegisterSW.mockReset();
  mockUseRegisterSW.mockImplementation(() => ({
    needRefresh: [mockNeedRefresh, mockSetNeedRefresh],
    offlineReady: [false, vi.fn()],
    updateServiceWorker: mockUpdateServiceWorker,
  }));
});

describe('UpdateAvailableBanner', () => {
  it('renders nothing when no update is waiting', () => {
    mockNeedRefresh = false;
    const { container } = render(<UpdateAvailableBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the banner with Update and Later buttons when an update is waiting', () => {
    mockNeedRefresh = true;
    render(<UpdateAvailableBanner />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/new version of Clawbolt is available/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /dismiss update notification/i })).toBeInTheDocument();
  });

  it('calls updateServiceWorker(true) when Update is clicked', async () => {
    mockNeedRefresh = true;
    const user = userEvent.setup();
    render(<UpdateAvailableBanner />);
    await user.click(screen.getByRole('button', { name: 'Update' }));
    expect(mockUpdateServiceWorker).toHaveBeenCalledWith(true);
  });

  it('hides the banner via setNeedRefresh(false) when Later is clicked', async () => {
    mockNeedRefresh = true;
    const user = userEvent.setup();
    render(<UpdateAvailableBanner />);
    await user.click(screen.getByRole('button', { name: /dismiss update notification/i }));
    expect(mockSetNeedRefresh).toHaveBeenCalledWith(false);
  });

  it('schedules a periodic update poll inside onRegisteredSW', () => {
    vi.useFakeTimers();
    try {
      mockNeedRefresh = false;
      const update = vi.fn().mockResolvedValue(undefined);
      const registration = { installing: null, update } as unknown as ServiceWorkerRegistration;
      mockUseRegisterSW.mockImplementation((options: { onRegisteredSW?: (url: string, r: ServiceWorkerRegistration) => void }) => {
        options.onRegisteredSW?.('/sw.js', registration);
        return {
          needRefresh: [false, mockSetNeedRefresh],
          offlineReady: [false, vi.fn()],
          updateServiceWorker: mockUpdateServiceWorker,
        };
      });
      render(<UpdateAvailableBanner />);
      expect(update).not.toHaveBeenCalled();
      vi.advanceTimersByTime(60 * 60 * 1000);
      expect(update).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(60 * 60 * 1000);
      expect(update).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
