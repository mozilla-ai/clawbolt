import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

export function getPremiumRouteElements(): ReactNode {
  return null;
}

export function getLoginPageElement(): ReactNode {
  // OSS has no login, redirect to app
  return <Navigate to="/app" replace />;
}

export function getDefaultSettingsTab(_isPremium: boolean): string {
  return 'heartbeat';
}

export function shouldRedirectRootToApp(_isPremium: boolean): boolean {
  return true;
}

export function getFeatureRequestUrl(): string {
  return 'https://github.com/mozilla-ai/clawbolt/issues/new?title=Feature+request:+&labels=enhancement';
}

export function getReportIssueUrl(): string {
  return 'https://github.com/mozilla-ai/clawbolt/issues/new?title=Bug:+&labels=bug';
}
