import type { ReactNode } from 'react';
import { Route } from 'react-router-dom';
import MarketingLayout from '@/layouts/MarketingLayout';
import DocsLayout from '@/layouts/DocsLayout';
import LoginPage from '@/components/LoginPage';
import HomePage from '@/pages/marketing/HomePage';
import LegalPage from '@/pages/marketing/LegalPage';
import DocsPage from '@/pages/docs/DocsPage';

// SPA login path. Mirrored in the OSS backend/app/web_paths.py for URL
// builders (transactional emails). The canonical route declaration is the
// `<Route path="/app/login">` in OSS App.tsx; everything else should import
// this constant.
export const LOGIN_PATH = '/app/login';

export function getLoginPageElement(): ReactNode {
  return <LoginPage />;
}

export function getPremiumRouteElements(): ReactNode {
  return (
    <>
      <Route element={<MarketingLayout />}>
        <Route index element={<HomePage />} />
      </Route>
      {/* Prose comes from public/legal/, which the deployment supplies.
          See LegalPage for why it is not checked in here. */}
      <Route
        path="/terms"
        element={<LegalPage src="/legal/terms.html" title="Terms of Service" />}
      />
      <Route
        path="/privacy"
        element={<LegalPage src="/legal/privacy.html" title="Privacy Policy" />}
      />
      <Route path="/docs" element={<DocsLayout />}>
        <Route index element={<DocsPage />} />
        <Route path="*" element={<DocsPage />} />
      </Route>
    </>
  );
}

export function getDefaultSettingsTab(_isPremium: boolean): string {
  // Channels is visible in every premium variant (non-premium, premium-user,
  // premium-admin), so it's a safe default for /app/settings with no tab.
  return 'channels';
}

export function shouldRedirectRootToApp(isPremium: boolean): boolean {
  return !isPremium;
}

/**
 * Admins land on the admin overview instead of their personal dashboard.
 *
 * Operating the platform is what an admin opens the app to do; their own
 * assistant is one sidebar click away. Non-admins get the OSS default
 * (null), and OSS's ``DefaultRedirect`` still sends anyone with incomplete
 * onboarding to the wizard first.
 */
export function getDefaultAppPath(isAdmin: boolean): string | null {
  return isAdmin ? '/app/admin' : null;
}

export function getFeatureRequestUrl(): string {
  return 'mailto:support@clawbolt.ai?subject=Feature+request:+';
}

export function getReportIssueUrl(): string {
  return 'mailto:support@clawbolt.ai?subject=Issue+report:+';
}

export function getDocsUrl(): string {
  return '/docs/guide/';
}
