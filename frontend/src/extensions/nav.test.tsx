import { render } from '@testing-library/react';
import { getExtraNavItems } from '@/extensions/nav';

const APPROVALS_SHIELD_CHECK_PATH_PREFIX = 'M9 12l2 2 4-4';

describe('getExtraNavItems', () => {
  it('returns an Admin nav item pointing at /app/admin', () => {
    const items = getExtraNavItems(false, true);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ to: '/app/admin', label: 'Admin' });
  });

  it('renders the Admin icon with a different SVG path than the Approvals shield-check', () => {
    const items = getExtraNavItems(false, true);
    const Icon = items[0]!.icon;
    const { container } = render(<Icon />);
    const path = container.querySelector('path')?.getAttribute('d') ?? '';
    expect(path).not.toBe('');
    expect(path.startsWith(APPROVALS_SHIELD_CHECK_PATH_PREFIX)).toBe(false);
  });
});
