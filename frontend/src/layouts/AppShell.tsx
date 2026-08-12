import { useState, useCallback, useEffect, type ComponentType } from 'react';
import { Outlet, NavLink, Navigate, useLocation } from 'react-router-dom';
import { ToastProvider } from '@heroui/toast';
import { useQueryClient } from '@tanstack/react-query';
import Button from '@/components/ui/button';
import ThemeToggle from '@/components/ui/ThemeToggle';
import OfflineIndicator from '@/components/ui/OfflineIndicator';
import { Spinner } from '@heroui/spinner';
import { useAuth } from '@/contexts/AuthContext';
import { ChatActivityProvider } from '@/contexts/ChatActivityContext';
import { Tooltip } from '@heroui/tooltip';
import { Link } from '@heroui/link';
import { Divider } from '@heroui/divider';
import { getFeatureRequestUrl, getReportIssueUrl, getDocsUrl, getExtraNavItems, renderSidebarFooter, renderHeaderBadge, type NavExtensionItem } from '@/extensions';
import useSwipeSidebar from '@/hooks/useSwipeSidebar';
import { useProfile } from '@/hooks/queries';
import { queryKeys } from '@/lib/query-keys';
import type { UserProfileResponse } from '@/types';

/** Context value provided to child routes via useOutletContext(). */
export interface AppShellContext {
  profile: UserProfileResponse | null;
  reloadProfile: () => void;
  isPremium: boolean;
  isAdmin: boolean;
}

const NAV_TOP = [
  { to: '/app/dashboard', label: 'Dashboard', icon: DashboardIcon, end: false },
] as const;

const NAV_MAIN = [
  { to: '/app/tools', label: 'Integrations', icon: ToolsIcon, end: false },
  { to: '/app/permissions', label: 'Approvals', icon: ApprovalsIcon, end: false },
  { to: '/app/settings', label: 'Settings', icon: SettingsIcon, end: false },
] as const;

const NAV_PERSONALIZE = [
  { to: '/app/memory', label: 'Knowledge', icon: MemoryIcon, end: false },
  { to: '/app/heartbeat', label: 'Priorities', icon: HeartbeatIcon, end: false },
  { to: '/app/soul', label: 'Personality', icon: SoulIcon, end: false },
  { to: '/app/user', label: 'About You', icon: UserIcon, end: false },
] as const;

const NAV_BOTTOM = [
  { to: '/app/chat', label: 'Chat', icon: ChatIcon, end: false },
] as const;

// ---------------------------------------------------------------------------
// Nav primitives
// ---------------------------------------------------------------------------

/** Row styling for every sidebar link, so top-level and nested rows stay in sync. */
function navRowClass(isActive: boolean, indented = false): string {
  return `flex items-center gap-3 px-3 ${indented ? 'pl-6' : ''} py-2 rounded-md text-sm transition-all duration-150 ${
    isActive
      ? 'bg-selected-bg text-primary font-medium border-l-2 border-primary'
      : 'text-muted-foreground border-l-2 border-transparent can-hover:hover:bg-secondary-hover can-hover:hover:text-foreground'
  }`;
}

function NavItemLink({
  item,
  indented = false,
  end = false,
  onNavigate,
}: {
  item: NavExtensionItem;
  indented?: boolean;
  end?: boolean;
  onNavigate: () => void;
}) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={end}
      onClick={() => {
        item.onClick?.();
        onNavigate();
      }}
      className={({ isActive }) => navRowClass(isActive, indented)}
    >
      <Icon />
      {item.label}
    </NavLink>
  );
}

/** True when ``pathname`` is ``to`` or a descendant of it. */
function isWithin(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}

/**
 * Collapsible sidebar fold. Two shapes, one implementation:
 *
 * - No ``to``: the header is a pure toggle (the built-in "Personalize" group).
 * - With ``to``: the header is a real link plus a separate chevron button, so
 *   clicking the label navigates to the section landing page while the chevron
 *   expands/collapses without navigating.
 *
 * The fold auto-opens whenever the current URL is inside the group, so a deep
 * link lands with the group already open. It stays collapsible after that.
 */
function NavGroup({
  label,
  icon: Icon,
  to,
  items,
  onNavigate,
}: {
  label: string;
  icon?: ComponentType;
  to?: string;
  items: NavExtensionItem[];
  onNavigate: () => void;
}) {
  const { pathname } = useLocation();
  const containsActive =
    (to != null && isWithin(pathname, to)) ||
    items.some(child => isWithin(pathname, child.to));
  const [open, setOpen] = useState(containsActive);

  useEffect(() => {
    if (containsActive) setOpen(true);
  }, [containsActive]);

  const toggle = useCallback(() => setOpen(v => !v), []);

  return (
    <>
      {to == null ? (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          className="flex items-center gap-3 px-3 py-2 rounded-md text-sm text-muted-foreground border-l-2 border-transparent can-hover:hover:bg-secondary-hover can-hover:hover:text-foreground transition-all duration-150 w-full"
        >
          <ChevronIcon open={open} />
          {label}
        </button>
      ) : (
        <div className="flex items-center">
          <NavLink
            to={to}
            end
            onClick={onNavigate}
            // Collapsed, the children are hidden, so the parent has to carry
            // the "you are here" highlight for the whole section. Expanded,
            // the active child carries it and the parent stays quiet.
            className={({ isActive }) =>
              `${navRowClass(isActive || (!open && containsActive))} flex-1 min-w-0`
            }
          >
            {Icon && <Icon />}
            {label}
          </NavLink>
          <button
            type="button"
            onClick={toggle}
            aria-expanded={open}
            aria-label={`${open ? 'Collapse' : 'Expand'} ${label}`}
            className="p-1 mr-1 rounded-md text-muted-foreground can-hover:hover:bg-secondary-hover can-hover:hover:text-foreground transition-all duration-150"
          >
            <ChevronIcon open={open} />
          </button>
        </div>
      )}
      {open &&
        items.map(child => (
          <NavItemLink key={child.to} item={child} indented onNavigate={onNavigate} />
        ))}
    </>
  );
}

export default function AppShell() {
  const { authState, currentAuthUser, isPremium, handleLogout } = useAuth();
  const queryClient = useQueryClient();
  // Gate the profile query until auth has resolved. Without this, the query
  // fires on every render while authState is still 'loading' or 'login' and
  // hammers /api/user/profile with 401 retries before the redirect below
  // kicks in.
  const {
    data: profile,
    isError: profileError,
    isPending: profilePending,
    refetch: refetchProfile,
  } = useProfile({ enabled: authState === 'ready' });
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const openSidebar = useCallback(() => setSidebarOpen(true), []);
  const closeSidebar = useCallback(() => setSidebarOpen(false), []);

  useSwipeSidebar({ isOpen: sidebarOpen, onOpen: openSidebar, onClose: closeSidebar });

  // Prevent iOS Safari auto-zoom on input focus. Since iOS 10, maximum-scale=1
  // only blocks automatic zoom (not user pinch-zoom), so accessibility is preserved.
  // Applied only on iOS to avoid disabling pinch-zoom on Android.
  useEffect(() => {
    if (!/iPhone|iPad|iPod/.test(navigator.userAgent)) return;
    const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]');
    if (meta && !meta.content.includes('maximum-scale')) {
      meta.setAttribute('content', meta.content + ', maximum-scale=1');
    }
  }, []);

  const reloadProfile = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.profile });
  }, [queryClient]);

  // Redirect to login if not authenticated
  if (authState === 'login') {
    return <Navigate to="/app/login" replace />;
  }

  if (authState === 'loading' || (profilePending && !profile)) {
    return (
      <div className="flex items-center justify-center min-h-dvh">
        <Spinner color="primary" size="md" aria-label="Loading" />
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

  const isAdmin = currentAuthUser?.role === 'admin';
  const extraNavItems = getExtraNavItems(isPremium, isAdmin);

  const ctx: AppShellContext = {
    profile: profile ?? null,
    reloadProfile,
    isPremium,
    isAdmin,
  };

  return (
    <ChatActivityProvider>
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
        className={`fixed md:static z-50 top-0 left-0 h-full w-56 bg-card border-r border-border flex flex-col transition-transform md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="p-4 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/clawbolt.png" alt="" className="w-7 h-7" />
            <h1 className="text-lg font-bold font-display text-foreground">Clawbolt</h1>
            {renderHeaderBadge()}
          </div>
        </div>

        <nav className="p-2 space-y-0.5">
          {NAV_TOP.map(item => (
            <NavItemLink key={item.to} item={item} end={item.end} onNavigate={closeSidebar} />
          ))}
          <Divider className="my-1" />
          {NAV_MAIN.map(item => (
            <NavItemLink key={item.to} item={item} end={item.end} onNavigate={closeSidebar} />
          ))}
          {extraNavItems.map(item =>
            item.children && item.children.length > 0 ? (
              <NavGroup
                key={item.to}
                label={item.label}
                icon={item.icon}
                to={item.to}
                items={item.children}
                onNavigate={closeSidebar}
              />
            ) : (
              <NavItemLink key={item.to} item={item} onNavigate={closeSidebar} />
            ),
          )}
          <NavGroup label="Personalize" items={[...NAV_PERSONALIZE]} onNavigate={closeSidebar} />
          <Divider className="my-1" />
          {NAV_BOTTOM.map(item => (
            <NavItemLink key={item.to} item={item} end={item.end} onNavigate={closeSidebar} />
          ))}
        </nav>

        <div className="flex-1" />

        {renderSidebarFooter({ isPremium, handleLogout, closeSidebar }) ?? (
          <div className="p-2 text-xs text-muted-foreground space-y-1">
            <Divider className="mb-1" />
            <div className="flex items-center justify-between px-3 py-1">
              <span>Theme</span>
              <ThemeToggle />
            </div>
            <NavLink
              to="/app/get-started"
              onClick={closeSidebar}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3 py-2 rounded-md text-xs transition-all duration-150 ${
                  isActive
                    ? 'text-primary font-medium'
                    : 'text-muted-foreground can-hover:hover:text-foreground'
                }`
              }
            >
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              Get Started
            </NavLink>
            <div className="flex gap-2 px-3 py-1 flex-wrap">
              <Link
                href={getDocsUrl()}
                isExternal
                size="sm"
                className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
              >
                Help
              </Link>
              <Link
                href={getReportIssueUrl()}
                isExternal
                size="sm"
                className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
              >
                Report issue
              </Link>
              <Link
                href={getFeatureRequestUrl()}
                isExternal
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
        )}
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
          <h1 className="text-lg font-bold font-display text-foreground">Clawbolt</h1>
          {renderHeaderBadge()}
        </header>

        <main className="flex-1 min-h-0 overflow-y-auto p-4 sm:p-6 max-w-7xl w-full mx-auto">
          <Outlet context={ctx} />
        </main>
      </div>

      <OfflineIndicator />
      <ToastProvider placement="bottom-right" />
    </div>
    </ChatActivityProvider>
  );
}

// --- Nav icons (inline SVG) ---

function DashboardIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM14 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zM14 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className={`w-5 h-5 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5l7 7-7 7" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
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

function HeartbeatIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3.343 7.778a4.5 4.5 0 016.364 0L12 10.07l2.293-2.293a4.5 4.5 0 116.364 6.364L12 22.485l-8.657-8.343a4.5 4.5 0 010-6.364z" />
    </svg>
  );
}

function SoulIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
    </svg>
  );
}

function ApprovalsIcon() {
  return (
    <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
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

