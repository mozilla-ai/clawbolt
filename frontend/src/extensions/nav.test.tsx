import { render } from '@testing-library/react';
import { ADMIN_NAV_ITEM } from '@/extensions/admin-nav-item';
import { getExtraNavItems } from '@/extensions/nav';

const APPROVALS_SHIELD_CHECK_PATH_PREFIX = 'M9 12l2 2 4-4';

describe('ADMIN_NAV_ITEM', () => {
  it('points at /app/admin and is labeled Admin', () => {
    expect(ADMIN_NAV_ITEM).toMatchObject({ to: '/app/admin', label: 'Admin' });
  });

  it('renders an icon whose SVG path is not the Approvals shield-check', () => {
    const Icon = ADMIN_NAV_ITEM.icon;
    const { container } = render(<Icon />);
    const path = container.querySelector('path')?.getAttribute('d') ?? '';
    expect(path).not.toBe('');
    expect(path.startsWith(APPROVALS_SHIELD_CHECK_PATH_PREFIX)).toBe(false);
  });
});

describe('getExtraNavItems', () => {
  it('returns the shared admin nav item', () => {
    const items = getExtraNavItems(false, true);
    expect(items).toEqual([ADMIN_NAV_ITEM]);
  });
});
