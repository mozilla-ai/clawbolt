import { Routes, Route, Navigate } from 'react-router-dom';
import Spinner from '@/components/ui/spinner';
import AppShell from '@/layouts/AppShell';
import OverviewPage from '@/pages/OverviewPage';
import ConversationsPage from '@/pages/ConversationsPage';
import MemoryPage from '@/pages/MemoryPage';
import SettingsPage from '@/pages/SettingsPage';
import ChatPage from '@/pages/ChatPage';
import ChecklistPage from '@/pages/ChecklistPage';
import { useAuth } from '@/contexts/AuthContext';
import {
  getLoginPageElement,
  getPremiumRouteElements,
  getDefaultSettingsTab,
  shouldRedirectRootToApp,
} from '@/extensions';

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
        <Route index element={<OverviewPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="conversations" element={<ConversationsPage />} />
        <Route path="conversations/:sessionId" element={<ConversationsPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="checklist" element={<ChecklistPage />} />
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
  );
}
