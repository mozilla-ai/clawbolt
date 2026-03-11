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
const SettingsPage = lazy(() => import('@/pages/SettingsPage'));
const ChecklistPage = lazy(() => import('@/pages/ChecklistPage'));
const ChannelsPage = lazy(() => import('@/pages/ChannelsPage'));
const ToolsPage = lazy(() => import('@/pages/ToolsPage'));

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

  const suspenseFallback = (
    <div className="flex justify-center items-center py-12">
      <Spinner />
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
          <Route index element={<Navigate to="/app/chat" replace />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="conversations" element={<ConversationsPage />} />
          <Route path="conversations/:sessionId" element={<ConversationsPage />} />
          <Route path="memory" element={<MemoryPage />} />
          <Route path="checklist" element={<ChecklistPage />} />
          <Route path="channels" element={<ChannelsPage />} />
          <Route path="tools" element={<ToolsPage />} />
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
