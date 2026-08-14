import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import ConsentBadge from './ConsentBadge';

describe('ConsentBadge', () => {
  it('renders nothing when consentAt is null or undefined', () => {
    const { container, rerender } = render(<ConsentBadge consentAt={null} />);
    expect(container.firstChild).toBeNull();

    rerender(<ConsentBadge consentAt={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders "shared" label by default', () => {
    render(<ConsentBadge consentAt="2026-01-01T00:00:00Z" />);
    expect(screen.getByText('shared')).toBeInTheDocument();
  });

  it('hides the text in compact mode but keeps the aria-label', () => {
    render(<ConsentBadge consentAt="2026-01-01T00:00:00Z" compact />);
    expect(screen.queryByText('shared')).not.toBeInTheDocument();
    // aria-label should still be present so screen readers see the badge.
    expect(screen.getByLabelText(/Shared data/)).toBeInTheDocument();
  });
});
