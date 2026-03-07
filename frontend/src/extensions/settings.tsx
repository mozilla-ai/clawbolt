import type { ReactNode } from 'react';

export interface ExtensionTab {
  key: string;
  label: string;
}

export function getExtraSettingsTabs(_isPremium: boolean, _isAdmin: boolean): ExtensionTab[] {
  return [];
}

export function renderPremiumSettingsTab(_key: string): ReactNode {
  return null;
}

export function showOssSettingsTabs(_isPremium: boolean): boolean {
  return true;
}
