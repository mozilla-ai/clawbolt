import { ADMIN_NAV_ITEM, type NavExtensionItem } from './admin-nav-item';

export type { NavExtensionItem };

export function getExtraNavItems(
  _isPremium: boolean,
  _isAdmin: boolean,
): NavExtensionItem[] {
  return [ADMIN_NAV_ITEM];
}
