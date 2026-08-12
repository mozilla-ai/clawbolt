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
  return 'model';
}

export function shouldRedirectRootToApp(_isPremium: boolean): boolean {
  return true;
}

/**
 * Landing path for ``/app``, or null to keep the default (get-started /
 * dashboard). Lets an extension route a role somewhere else on login;
 * premium sends admins to the admin overview.
 */
export function getDefaultAppPath(_isAdmin: boolean): string | null {
  return null;
}

export function getFeatureRequestUrl(): string {
  return 'https://github.com/mozilla-ai/clawbolt/issues/new?title=Feature+request:+&labels=enhancement';
}

export function getReportIssueUrl(): string {
  return 'https://github.com/mozilla-ai/clawbolt/issues/new?title=Bug:+&labels=bug';
}

export function getDocsUrl(): string {
  return 'https://clawbolt.ai/guide/';
}
