import { render } from '@testing-library/react';
import { ADMIN_NAV_ITEM } from '@/extensions/admin-nav-item';
import { ADMIN_SUB_PAGES, adminPath } from '@/extensions/admin/nav-items';
import { getExtraNavItems } from './nav';

describe('premium getExtraNavItems', () => {
  it('returns an empty list when the user is not an admin', () => {
    expect(getExtraNavItems(true, false)).toEqual([]);
  });

  it('returns the shared admin nav item when the user is an admin', () => {
    const items = getExtraNavItems(true, true);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      to: ADMIN_NAV_ITEM.to,
      label: ADMIN_NAV_ITEM.label,
      icon: ADMIN_NAV_ITEM.icon,
    });
  });

  it('renders the shared admin icon (not a stale duplicate)', () => {
    const items = getExtraNavItems(true, true);
    const Icon = items[0]!.icon;
    const { container } = render(<Icon />);
    const path = container.querySelector('path')?.getAttribute('d') ?? '';
    expect(path).not.toBe('');
    // Anchor: any path that begins with the old shield-check signature would
    // mean we slipped back to the pre-refactor duplicated icon.
    expect(path.startsWith('M9 12l2 2 4-4')).toBe(false);
  });

  // Each admin section is a sidebar row under the Admin fold (#662), so the
  // children have to stay in lockstep with the routes in admin/index.tsx.
  // ADMIN_SUB_PAGES is the shared source; this guards the mapping.
  it('exposes every admin sub-page as a child nav item', () => {
    const children = getExtraNavItems(true, true)[0]!.children;
    expect(children).toBeDefined();
    expect(children!.map(c => c.to)).toEqual(ADMIN_SUB_PAGES.map(p => adminPath(p.slug)));
    expect(children!.map(c => c.label)).toEqual(ADMIN_SUB_PAGES.map(p => p.label));
  });

  it('points every child at a path under the admin parent', () => {
    const item = getExtraNavItems(true, true)[0]!;
    for (const child of item.children ?? []) {
      expect(child.to.startsWith(`${item.to}/`)).toBe(true);
    }
  });

  it('leads with Overview, the admin landing page', () => {
    const children = getExtraNavItems(true, true)[0]!.children!;
    expect(children[0]).toMatchObject({ label: 'Overview', to: '/app/admin/overview' });
  });
});
