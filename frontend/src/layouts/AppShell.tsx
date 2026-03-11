import { useState, useEffect, useCallback } from 'react';
import { Outlet, NavLink, Navigate, Link as RouterLink, useSearchParams } from 'react-router-dom';
import { ToastProvider } from '@heroui/toast';
import { useQueryClient } from '@tanstack/react-query';
import api from '@/api';
import Button from '@/components/ui/button';
import OfflineIndicator from '@/components/ui/OfflineIndicator';
import Spinner from '@/components/ui/spinner';
import SearchOverlay from '@/components/SearchOverlay';
import { useAuth } from '@/contexts/AuthContext';
import { Tooltip } from '@heroui/tooltip';
import { Link } from '@heroui/link';
import { Divider } from '@heroui/divider';
import { getFeatureRequestUrl, getReportIssueUrl } from '@/extensions';
import useSwipeSidebar from '@/hooks/useSwipeSidebar';
import { useProfile } from '@/hooks/queries';
import { queryKeys } from '@/lib/query-keys';
import type { UserProfile, SessionSummary, MemoryFact } from '@/types';

/** Context value provided to child routes via useOutletContext(). */
export interface AppShellContext {
  profile: UserProfile | null;
  reloadProfile: () => void;
  isPremium: boolean;
  isAdmin: boolean;
}

const NAV_ITEMS = [
  { to: '/app/chat', label: 'Chat', icon: ChatIcon, end: false },
  { to: '/app/conversations', label: 'Conversations', icon: ConversationsIcon, end: false },
  { to: '/app/memory', label: 'Memory', icon: MemoryIcon, end: false },
  { to: '/app/checklist', label: 'Checklist', icon: ChecklistIcon, end: false },
  { to: '/app/channels', label: 'Channels', icon: ChannelsIcon, end: false },
  { to: '/app/tools', label: 'Tools', icon: ToolsIcon, end: false },
  { to: '/app/settings', label: 'Settings', icon: SettingsIcon, end: false },
] as const;

export default function AppShell() {
  const { authState, currentAuthUser, isPremium, handleLogout } = useAuth();
  const queryClient = useQueryClient();
  const {
    data: profile,
    isError: profileError,
    isPending: profilePending,
    refetch: refetchProfile,
  } = useProfile();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const [searchOpen, setSearchOpen] = useState(false);
  const [searchSessions, setSearchSessions] = useState<SessionSummary[]>([]);
  const [searchFacts, setSearchFacts] = useState<MemoryFact[]>([]);

  const openSidebar = useCallback(() => setSidebarOpen(true), []);
  const closeSidebar = useCallback(() => setSidebarOpen(false), []);

  useSwipeSidebar({ isOpen: sidebarOpen, onOpen: openSidebar, onClose: closeSidebar });

  const reloadProfile = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.profile });
  }, [queryClient]);

  // Global Cmd+K / Ctrl+K listener
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setSearchOpen(true);
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Fetch data when search overlay opens
  useEffect(() => {
    if (!searchOpen) return;
    api.listSessions(0, 50)
      .then((res) => setSearchSessions(res.sessions))
      .catch((err: unknown) => console.error('[AppShell] Failed to load sessions for search:', err));
    api.listMemoryFacts()
      .then(setSearchFacts)
      .catch((err: unknown) => console.error('[AppShell] Failed to load memory for search:', err));
  }, [searchOpen]);

  // Redirect to login if not authenticated
  if (authState === 'login') {
    return <Navigate to="/app/login" replace />;
  }

  if (authState === 'loading' || (profilePending && !profile)) {
    return (
      <div className="flex items-center justify-center min-h-dvh">
        <Spinner />
      </div>
    );
  }

  // Show error banner if profile loading failed and no cached data
  if (profileError && !profile) {
    return (
      <div className="flex flex-col items-center justify-center min-h-dvh gap-3 text-muted-foreground">
        <p className="text-sm">Unable to load your profile. The server may be unavailable.</p>
        <Button onClick={() => void refetchProfile()}>Retry</Button>
      </div>
    );
  }

  const ctx: AppShellContext = {
    profile: profile ?? null,
    reloadProfile,
    isPremium,
    isAdmin: currentAuthUser?.role === 'admin',
  };

  return (
    <div className="flex h-dvh">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={closeSidebar}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed md:static z-50 top-0 left-0 h-full w-64 bg-card border-r border-border flex flex-col transition-transform md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="p-4 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/clawbolt.png" alt="" className="w-7 h-7" />
            <h1 className="text-lg font-bold text-foreground">Clawbolt</h1>
          </div>
        </div>

        <nav className="p-2 space-y-0.5">
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={closeSidebar}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-all duration-150 ${
                  isActive
                    ? 'bg-selected-bg text-primary font-medium border-l-2 border-primary'
                    : 'text-muted-foreground border-l-2 border-transparent can-hover:hover:bg-secondary-hover can-hover:hover:text-foreground'
                }`
              }
            >
              <Icon />
              {label}
            </NavLink>
          ))}
        </nav>

        <RecentConversations onNavigate={closeSidebar} />

        <div className="p-2 text-xs text-muted-foreground space-y-1">
          <Divider className="mb-1" />
          <div className="flex gap-2 px-3 py-1">
            <Link
              href={getReportIssueUrl()}
              isExternal
              showAnchorIcon
              size="sm"
              className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              Report issue
            </Link>
            <Link
              href={getFeatureRequestUrl()}
              isExternal
              showAnchorIcon
              size="sm"
              className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              Feature request
            </Link>
          </div>
          {isPremium && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleLogout}
              className="w-full justify-start text-xs text-muted-foreground font-normal px-3"
            >
              Log out
            </Button>
          )}
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Mobile header */}
        <header className="md:hidden flex items-center gap-3 px-4 py-3 border-b border-border bg-card">
          <Tooltip content="Open menu" delay={400} closeDelay={0}>
            <Button
              variant="ghost"
              size="icon"
              onClick={openSidebar}
              aria-label="Open menu"
            >
              <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </Button>
          </Tooltip>
          <img src="/clawbolt.png" alt="" className="w-6 h-6" />
          <h1 className="text-lg font-bold text-foreground">Clawbolt</h1>
        </header>

        <main className="flex-1 overflow-y-auto p-4 sm:p-6 max-w-5xl w-full mx-auto">
          <Outlet context={ctx} />
        </main>
      </div>

      <OfflineIndicator />
      <ToastProvider placement="bottom-right" />

      <SearchOverlay
        isOpen={searchOpen}
        onClose={() => setSearchOpen(false)}
        sessions={searchSessions}
        memoryFacts={searchFacts}
      />
    </div>
  );
}

// --- Nav icons (inline SVG) ---

function ChatIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
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

function ChannelsIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  );
}

function ToolsIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
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

// --- Recent conversations sidebar section ---

/** Format an ISO timestamp as a short relative string (e.g. "5m ago", "2h ago"). */
export function formatRelativeTime(iso: string): string {
  const parsed = new Date(iso);
  if (isNaN(parsed.getTime())) return iso || 'Unknown';
  const diffMs = Date.now() - parsed.getTime();
  const seconds = Math.max(0, Math.floor(diffMs / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function RecentConversations({ onNavigate }: { onNavigate: () => void }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [searchParams] = useSearchParams();
  const activeSessionId = searchParams.get('session');

  useEffect(() => {
    api.listSessions(0, 10)
      .then((res) => setSessions(res.sessions))
      .catch((err: unknown) => {
        console.error('[RecentConversations] Failed to load sessions:', err);
      });
  }, []);

  if (sessions.length === 0) return null;

  return (
    <div className="flex-1 flex flex-col min-h-0 border-t border-border">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-xs font-medium text-muted-foreground">Recent</span>
        <RouterLink
          to="/app/conversations"
          onClick={onNavigate}
          className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
        >
          View all
        </RouterLink>
      </div>
      <div className="flex-1 overflow-y-auto px-1 pb-1" data-testid="recent-conversations">
        {sessions.map((s) => {
          const isActive = s.id === activeSessionId;
          return (
            <RouterLink
              key={s.id}
              to={`/app/chat?session=${encodeURIComponent(s.id)}`}
              onClick={onNavigate}
              className={`block px-3 py-1.5 rounded-md text-sm transition-all duration-150 ${
                isActive
                  ? 'bg-selected-bg text-primary border-l-2 border-primary'
                  : 'text-muted-foreground can-hover:hover:bg-secondary-hover can-hover:hover:text-foreground'
              }`}
            >
              <p className="line-clamp-1 text-xs">
                {s.last_message_preview || 'New conversation'}
              </p>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                {formatRelativeTime(s.start_time)}
              </p>
            </RouterLink>
          );
        })}
      </div>
    </div>
  );
}
