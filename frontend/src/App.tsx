import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate, useOutletContext } from 'react-router-dom';
import { Spinner } from '@heroui/spinner';
import AppShell, { type AppShellContext } from '@/layouts/AppShell';
import { useAuth } from '@/contexts/AuthContext';
import {
  getLoginPageElement,
  getPremiumRouteElements,
  getDefaultSettingsTab,
  shouldRedirectRootToApp,
  getAdminPageElement,
} from '@/extensions';

const DashboardPage = lazy(() => import('@/pages/DashboardPage'));
const ChatPage = lazy(() => import('@/pages/ChatPage'));
const MemoryPage = lazy(() => import('@/pages/MemoryPage'));
const SettingsPage = lazy(() => import('@/pages/SettingsPage'));
const HeartbeatPage = lazy(() => import('@/pages/HeartbeatPage'));
const SoulPage = lazy(() => import('@/pages/SoulPage'));
const UserPage = lazy(() => import('@/pages/UserPage'));
const ChannelsPage = lazy(() => import('@/pages/ChannelsPage'));
const ToolsPage = lazy(() => import('@/pages/ToolsPage'));
const OAuthCallbackPage = lazy(() => import('@/pages/OAuthCallbackPage'));
const GetStartedPage = lazy(() => import('@/pages/GetStartedPage'));

/** Redirect index to get-started if onboarding is incomplete, otherwise to chat. */
function DefaultRedirect() {
  const { profile } = useOutletContext<AppShellContext>();
  if (profile && !profile.onboarding_complete) {
    return <Navigate to="/app/get-started" replace />;
  }
  return <Navigate to="/app/dashboard" replace />;
}

/** Render admin page from extension, or redirect if not admin. */
function AdminRoute() {
  const { isAdmin } = useOutletContext<AppShellContext>();
  const element = getAdminPageElement(isAdmin);
  if (!element) return <Navigate to="/app" replace />;
  return <>{element}</>;
}

export default function App() {
  const { authState, isPremium } = useAuth();

  if (authState === 'loading') {
    return (
      <div className="flex flex-col items-center justify-center min-h-dvh gap-3 text-muted-foreground">
        <Spinner color="primary" size="md" aria-label="Loading" />
        <span className="text-sm">Loading...</span>
      </div>
    );
  }

  const suspenseFallback = (
    <div className="flex justify-center items-center py-12">
      <Spinner color="primary" size="md" aria-label="Loading" />
    </div>
  );

  return (
    <Suspense fallback={suspenseFallback}>
      <Routes>
        {/* Premium route elements (marketing pages, etc.) */}
        {getPremiumRouteElements()}

        {/* Login: OSS redirects to /app, premium renders its LoginPage */}
        <Route path="/app/login" element={getLoginPageElement()} />

        {/* Authenticated app */}
        <Route path="/app" element={<AppShell />}>
          <Route index element={<DefaultRedirect />} />
          <Route path="get-started" element={<GetStartedPage />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="memory" element={<MemoryPage />} />
          <Route path="heartbeat" element={<HeartbeatPage />} />
          <Route path="soul" element={<SoulPage />} />
          <Route path="user" element={<UserPage />} />
          <Route path="channels" element={<ChannelsPage />} />
          <Route path="tools" element={<ToolsPage />} />
          <Route path="oauth/callback" element={<OAuthCallbackPage />} />
          <Route path="admin" element={<AdminRoute />} />
          <Route path="settings/:tab" element={<SettingsPage />} />
          <Route path="settings" element={<Navigate to={`/app/settings/${getDefaultSettingsTab(isPremium)}`} replace />} />
        </Route>

        {/* OSS root redirects to app; premium root handled by extension routes */}
        {shouldRedirectRootToApp(isPremium) && <Route path="/" element={<Navigate to="/app" replace />} />}

        {/* 404 */}
        <Route path="*" element={
          <div className="flex flex-col items-center justify-center min-h-dvh gap-3 text-muted-foreground">
            <p className="text-lg font-semibold">404</p>
            <p className="text-sm">Page not found</p>
            <a href="/app" className="text-sm text-primary hover:underline">Go to dashboard</a>
          </div>
        } />
      </Routes>
    </Suspense>
  );
}
