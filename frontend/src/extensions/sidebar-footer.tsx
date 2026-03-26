import type { ReactNode } from 'react';

export interface SidebarFooterProps {
  isPremium: boolean;
  handleLogout: () => void;
  closeSidebar: () => void;
}

export function renderSidebarFooter(_props: SidebarFooterProps): ReactNode {
  return null;
}
