import { useState, useEffect, useCallback } from 'react';
import { Outlet, NavLink, Navigate } from 'react-router-dom';
import { Toaster } from 'sonner';
import api from '@/api';
import Button from '@/components/ui/button';
import Spinner from '@/components/ui/spinner';
import { useAuth } from '@/contexts/AuthContext';
import { getFeatureRequestUrl, getReportIssueUrl } from '@/extensions';
import type { ContractorProfile } from '@/types';

/** Context value provided to child routes via useOutletContext(). */
export interface AppShellContext {
  profile: ContractorProfile | null;
  reloadProfile: () => void;
  isPremium: boolean;
  isAdmin: boolean;
}

const NAV_ITEMS = [
  { to: '/app', label: 'Overview', icon: OverviewIcon, end: true },
  { to: '/app/conversations', label: 'Conversations', icon: ConversationsIcon, end: false },
  { to: '/app/memory', label: 'Memory', icon: MemoryIcon, end: false },
  { to: '/app/checklist', label: 'Checklist', icon: ChecklistIcon, end: false },
  { to: '/app/settings', label: 'Settings', icon: SettingsIcon, end: false },
] as const;

export default function AppShell() {
  const { authState, currentAuthUser, isPremium, handleLogout } = useAuth();
  const [profile, setProfile] = useState<ContractorProfile | null>(null);
  const [profileError, setProfileError] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const loadProfile = useCallback(() => {
    setProfileError(false);
    api.getProfile()
      .then(setProfile)
      .catch((err: unknown) => {
        console.error('[AppShell] Failed to load profile:', err);
        setProfileError(true);
      });
  }, []);

  useEffect(() => {
    if (authState !== 'ready') return;
    loadProfile();
  }, [authState, loadProfile]);

  // Redirect to login if not authenticated
  if (authState === 'login') {
    return <Navigate to="/app/login" replace />;
  }

  if (authState === 'loading') {
    return (
      <div className="flex items-center justify-center min-h-dvh">
        <Spinner />
      </div>
    );
  }

  // Show error banner if profile loading failed
  if (profileError) {
    return (
      <div className="flex flex-col items-center justify-center min-h-dvh gap-3 text-muted-foreground">
        <p className="text-sm">Unable to load your profile. The server may be unavailable.</p>
        <Button onClick={loadProfile}>Retry</Button>
      </div>
    );
  }

  const ctx: AppShellContext = {
    profile,
    reloadProfile: loadProfile,
    isPremium,
    isAdmin: currentAuthUser?.role === 'admin',
  };

  return (
    <div className="flex h-dvh">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed md:static z-50 top-0 left-0 h-full w-64 bg-card border-r border-border flex flex-col transition-transform md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="p-4 border-b border-border">
          <h1 className="text-lg font-bold text-foreground">Clawbolt</h1>
          {profile && (
            <p className="text-xs text-muted-foreground mt-0.5 truncate">
              {profile.name || profile.trade || 'Dashboard'}
            </p>
          )}
        </div>

        <nav className="flex-1 p-2 space-y-0.5 overflow-y-auto">
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2 rounded-[--radius-md] text-sm transition-colors ${
                  isActive
                    ? 'bg-selected-bg text-primary font-medium'
                    : 'text-muted-foreground hover:bg-secondary-hover hover:text-foreground'
                }`
              }
            >
              <Icon />
              {label}
            </NavLink>
          ))}
        </nav>

        {!profile?.onboarding_complete && (
          <div className="mx-2 mb-2 p-3 rounded-[--radius-md] bg-info-bg border border-info-border text-info-text text-xs">
            Connect with your assistant on Telegram to get started.
          </div>
        )}

        <div className="p-2 border-t border-border text-xs text-muted-foreground space-y-1">
          <div className="flex gap-2 px-3 py-1">
            <a
              href={getReportIssueUrl()}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-foreground transition-colors"
            >
              Report issue
            </a>
            <a
              href={getFeatureRequestUrl()}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-foreground transition-colors"
            >
              Feature request
            </a>
          </div>
          {isPremium && (
            <button
              onClick={handleLogout}
              className="w-full text-left px-3 py-1 hover:text-foreground transition-colors"
            >
              Log out
            </button>
          )}
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Mobile header */}
        <header className="md:hidden flex items-center gap-3 px-4 py-3 border-b border-border bg-card">
          <button
            onClick={() => setSidebarOpen(true)}
            className="text-foreground"
            aria-label="Open menu"
          >
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <h1 className="text-lg font-bold text-foreground">Clawbolt</h1>
        </header>

        <main className="flex-1 overflow-y-auto p-4 sm:p-6 max-w-5xl w-full mx-auto">
          <Outlet context={ctx} />
        </main>
      </div>

      <Toaster position="bottom-right" richColors />
    </div>
  );
}

// --- Nav icons (inline SVG) ---

function OverviewIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1h-2z" />
    </svg>
  );
}

function ConversationsIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
    </svg>
  );
}

function MemoryIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
    </svg>
  );
}

function ChecklistIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}
