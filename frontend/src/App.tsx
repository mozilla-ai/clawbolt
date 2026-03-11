import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Spinner from '@/components/ui/spinner';
import AppShell from '@/layouts/AppShell';
import { useAuth } from '@/contexts/AuthContext';
import {
  getLoginPageElement,
  getPremiumRouteElements,
  getDefaultSettingsTab,
  shouldRedirectRootToApp,
} from '@/extensions';

const ChatPage = lazy(() => import('@/pages/ChatPage'));
const ConversationsPage = lazy(() => import('@/pages/ConversationsPage'));
const MemoryPage = lazy(() => import('@/pages/MemoryPage'));
const ChecklistPage = lazy(() => import('@/pages/ChecklistPage'));
const ChannelsPage = lazy(() => import('@/pages/ChannelsPage'));
const ToolsPage = lazy(() => import('@/pages/ToolsPage'));
const SettingsPage = lazy(() => import('@/pages/SettingsPage'));

function PageSuspense({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export default function App() {
  const { authState, isPremium } = useAuth();

  if (authState === 'loading') {
    return (
      <div className="flex flex-col items-center justify-center min-h-dvh gap-3 text-muted-foreground">
        <Spinner />
        <span className="text-sm">Loading...</span>
      </div>
    );
  }

  return (
    <Routes>
      {/* Premium route elements (marketing pages, etc.) */}
      {getPremiumRouteElements()}

      {/* Login: OSS redirects to /app, premium renders its LoginPage */}
      <Route path="/app/login" element={getLoginPageElement()} />

      {/* Authenticated app */}
      <Route path="/app" element={<AppShell />}>
        <Route index element={<Navigate to="/app/chat" replace />} />
        <Route path="chat" element={<PageSuspense><ChatPage /></PageSuspense>} />
        <Route path="conversations" element={<PageSuspense><ConversationsPage /></PageSuspense>} />
        <Route path="conversations/:sessionId" element={<PageSuspense><ConversationsPage /></PageSuspense>} />
        <Route path="memory" element={<PageSuspense><MemoryPage /></PageSuspense>} />
        <Route path="checklist" element={<PageSuspense><ChecklistPage /></PageSuspense>} />
        <Route path="channels" element={<PageSuspense><ChannelsPage /></PageSuspense>} />
        <Route path="tools" element={<PageSuspense><ToolsPage /></PageSuspense>} />
        <Route path="settings/:tab" element={<PageSuspense><SettingsPage /></PageSuspense>} />
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
  );
}
