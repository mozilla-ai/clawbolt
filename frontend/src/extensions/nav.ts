import { ADMIN_NAV_ITEM, type NavExtensionItem } from '@/extensions/admin-nav-item';
import { ADMIN_SUB_PAGES, adminPath } from '@/extensions/admin/nav-items';

export type { NavExtensionItem };

export function getExtraNavItems(
  _isPremium: boolean,
  isAdmin: boolean,
): NavExtensionItem[] {
  if (!isAdmin) return [];
  return [
    {
      ...ADMIN_NAV_ITEM,
      // Each admin sub-page is a real route, so it gets its own sidebar row
      // under the Admin fold. AppShell auto-expands the fold whenever the
      // current URL is inside /app/admin.
      children: ADMIN_SUB_PAGES.map(page => ({
        to: adminPath(page.slug),
        label: page.label,
        icon: page.icon,
      })),
    },
  ];
}
