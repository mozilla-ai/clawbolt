import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import DataSharingConsentSection from './DataSharingConsentSection';

const mockGet = vi.fn();
const mockUpdate = vi.fn();

vi.mock('@/api', () => ({
  default: {
    getDataSharingConsent: (...args: unknown[]) => mockGet(...args),
    updateDataSharingConsent: (...args: unknown[]) => mockUpdate(...args),
  },
}));

vi.mock('@/lib/toast', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DataSharingConsentSection', () => {
  it('renders an unchecked checkbox when consent is false', async () => {
    mockGet.mockResolvedValue({
      data_sharing_consent: false,
      data_sharing_consent_at: null,
    });

    renderWithRouter(<DataSharingConsentSection />);

    const checkbox = await screen.findByRole('checkbox');
    expect(checkbox).not.toBeChecked();
    // No "last updated" line when there's no timestamp.
    expect(screen.queryByText(/Last updated/i)).not.toBeInTheDocument();
  });

  it('renders a checked checkbox + last-updated line when consent is true', async () => {
    mockGet.mockResolvedValue({
      data_sharing_consent: true,
      data_sharing_consent_at: '2026-04-15T12:34:56+00:00',
    });

    renderWithRouter(<DataSharingConsentSection />);

    const checkbox = await screen.findByRole('checkbox');
    expect(checkbox).toBeChecked();
    expect(await screen.findByText(/Last updated/i)).toBeInTheDocument();
  });

  it('toggling on calls updateDataSharingConsent({ consent: true })', async () => {
    mockGet.mockResolvedValue({
      data_sharing_consent: false,
      data_sharing_consent_at: null,
    });
    mockUpdate.mockResolvedValue({
      data_sharing_consent: true,
      data_sharing_consent_at: '2026-05-01T00:00:00+00:00',
    });

    const user = userEvent.setup();
    renderWithRouter(<DataSharingConsentSection />);

    const checkbox = await screen.findByRole('checkbox');
    await user.click(checkbox);

    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ consent: true });
    });
  });

  it('toggling off calls updateDataSharingConsent({ consent: false })', async () => {
    mockGet.mockResolvedValue({
      data_sharing_consent: true,
      data_sharing_consent_at: '2026-04-15T12:34:56+00:00',
    });
    mockUpdate.mockResolvedValue({
      data_sharing_consent: false,
      data_sharing_consent_at: '2026-05-01T00:00:00+00:00',
    });

    const user = userEvent.setup();
    renderWithRouter(<DataSharingConsentSection />);

    const checkbox = await screen.findByRole('checkbox');
    await user.click(checkbox);

    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ consent: false });
    });
  });

  it('renders a custom heading when provided', async () => {
    mockGet.mockResolvedValue({
      data_sharing_consent: false,
      data_sharing_consent_at: null,
    });

    renderWithRouter(<DataSharingConsentSection heading="Custom title" />);

    expect(await screen.findByText('Custom title')).toBeInTheDocument();
  });
});
