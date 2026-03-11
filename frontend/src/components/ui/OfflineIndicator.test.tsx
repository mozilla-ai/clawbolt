import { render, screen, act } from '@testing-library/react';
import OfflineIndicator from '@/components/ui/OfflineIndicator';

describe('OfflineIndicator', () => {
  let originalOnLine: boolean;

  beforeEach(() => {
    originalOnLine = navigator.onLine;
  });

  afterEach(() => {
    Object.defineProperty(navigator, 'onLine', {
      value: originalOnLine,
      writable: true,
      configurable: true,
    });
  });

  it('does not render when online', () => {
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    });

    render(<OfflineIndicator />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('renders offline banner when browser is offline', () => {
    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    });

    render(<OfflineIndicator />);
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.getByText(/you are offline/i)).toBeInTheDocument();
  });

  it('shows banner when offline event fires', () => {
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    });

    render(<OfflineIndicator />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.getByText(/you are offline/i)).toBeInTheDocument();
  });

  it('hides banner when online event fires after being offline', () => {
    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    });

    render(<OfflineIndicator />);
    expect(screen.getByRole('status')).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event('online'));
    });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
