import { useState, useEffect, useCallback, useRef } from 'react';
import { Routes, Route, Navigate, useNavigate, useParams, useLocation } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { findAdminUserById, getAdminVersion, type AdminUser } from './admin-api';
import { ADMIN_BASE_PATH, adminPath, findAdminSubPage } from './nav-items';
import OverviewTab from './tabs/overview';
import UsersTab from './tabs/users';
import UserDetailView, { isUserDetailSection, type UserDetailSection } from './tabs/user-detail';
import ConfigTab from './tabs/config';
import AccessAndWaitlistTab from './tabs/access-and-waitlist';
import ReportedTab from './tabs/reported';
import ApiKeysTab from './tabs/api-keys';
import MonitoringTab from './tabs/monitoring';
import ModelEvalTab from './tabs/model-eval';

// --- Navigation model ---
//
// Each admin section is a route under /app/admin (#662). The horizontal tab
// bar is gone: the sections are sidebar rows under the "Admin" fold, which
// means every one of them is bookmarkable, back/forward works, and the
// sidebar shows where you are.
//
// The previous model was hash fragments (``#users``, ``#users/{id}``).
// ``legacyHashTarget`` below keeps those links working.

// ---------------------------------------------------------------------------
// Legacy hash deep links
// ---------------------------------------------------------------------------

/**
 * Translate a pre-#662 hash into the route that replaces it.
 *
 * Pure, and resolved during render rather than in an effect, for two reasons:
 *
 * - An effect-based redirect races the index route's ``<Navigate to="overview">``.
 *   StrictMode re-runs mounted effects, so the index redirect fires a second
 *   time after ours and drags the admin back to Overview. Deciding before
 *   ``<Routes>`` renders means the index route never matches while a legacy
 *   hash is pending.
 * - It must stay pure. An earlier version cleared the hash here; StrictMode's
 *   second render pass then saw an empty hash and returned null. Navigating to
 *   a hash-less path drops the fragment on its own, so there is nothing to clear.
 *
 * ``#users/{id}`` carries a user id; ``#shared`` was retired earlier and
 * already aliased onto Users. Anything unrecognized falls back to Overview.
 * Returns null once we are off the admin root, so the redirect fires once.
 */
function legacyHashTarget(pathname: string, rawHash: string): string | null {
  // A hash on a real sub-route is not ours to interpret.
  if (pathname !== ADMIN_BASE_PATH && pathname !== `${ADMIN_BASE_PATH}/`) return null;

  const hash = rawHash.replace('#', '');
  if (!hash) return null;

  const userMatch = hash.match(/^users\/(.+)$/);
  if (userMatch) return `${adminPath('users')}/${userMatch[1]}`;
  if (hash === 'shared' || hash.startsWith('shared/')) return adminPath('users');
  if (findAdminSubPage(hash)) return adminPath(hash);
  return adminPath('overview');
}

// ---------------------------------------------------------------------------
// Auto-reload on deploy
// ---------------------------------------------------------------------------
//
// vite-plugin-pwa's autoUpdate handles freshness on page-load (the new
// service worker takes over on the next navigation), but a tab kept open
// across a deploy never sees the new SW until the user navigates. This hook
// polls /api/admin/version and reloads the page when the backend's process
// start time changes, which fills the "kept open across deploy" gap.
//
// We key on ``started_at`` rather than commit SHA because every fresh
// process picks up a new value, so this works even when commit env vars
// are not stamped at build time. Network errors are swallowed silently;
// only a successful fetch with a different baseline triggers reload.

const VERSION_POLL_INTERVAL_MS = 60_000;

function useAutoReloadOnDeploy(): void {
  const baselineRef = useRef<string | null>(null);
  const reloadingRef = useRef(false);

  useEffect(() => {
    // Skip in dev: HMR / vite restarts would otherwise reload the page
    // every time the backend restarts, fighting the dev experience.
    if (import.meta.env.DEV) return;

    let cancelled = false;

    const tick = async () => {
      if (cancelled || reloadingRef.current) return;
      try {
        const v = await getAdminVersion();
        if (cancelled) return;
        if (baselineRef.current === null) {
          baselineRef.current = v.started_at;
          return;
        }
        if (v.started_at !== baselineRef.current) {
          reloadingRef.current = true;
          window.location.reload();
        }
      } catch {
        // Transient failure: try again next tick.
      }
    };

    void tick();
    const id = window.setInterval(tick, VERSION_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);
}

// ---------------------------------------------------------------------------
// Section chrome
// ---------------------------------------------------------------------------

/**
 * Heading + one-line description for a section.
 *
 * With the tab bar gone, this is what tells you which admin page you are on
 * when the sidebar is collapsed behind the mobile hamburger.
 */
function SectionHeader({ slug }: { slug: string }) {
  const page = findAdminSubPage(slug);
  if (!page) return null;
  return (
    <header className="mb-5">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Admin</p>
      <h2 className="text-xl font-semibold font-display">{page.label}</h2>
      <p className="text-muted-foreground text-sm mt-0.5">{page.description}</p>
    </header>
  );
}

function AdminSection({ slug, children }: { slug: string; children: React.ReactNode }) {
  return (
    <div>
      <SectionHeader slug={slug} />
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Route wrappers
// ---------------------------------------------------------------------------

function OverviewRoute() {
  const navigate = useNavigate();
  return (
    <AdminSection slug="overview">
      <OverviewTab
        onGoToSection={slug => navigate(adminPath(slug))}
        onSelectUserById={(userId, section) =>
          navigate(`${adminPath('users')}/${userId}${section ? `/${section}` : ''}`)
        }
      />
    </AdminSection>
  );
}

function UsersRoute({ currentUserId }: { currentUserId?: string }) {
  const navigate = useNavigate();
  return (
    <AdminSection slug="users">
      <UsersTab
        onSelectUser={user => navigate(`${adminPath('users')}/${user.id}`)}
        currentUserId={currentUserId}
      />
    </AdminSection>
  );
}

/**
 * Resolve ``:userId`` into the list-shaped row User-detail needs.
 *
 * ``/admin/users/{id}`` returns the detail projection, which deliberately
 * omits the consent snapshot and the month's message count. Those live only
 * on the list response, so we page the list until the row shows up rather
 * than adding a backend endpoint.
 */
function UserDetailRoute({ currentUserId }: { currentUserId?: string }) {
  const { userId, section } = useParams<{ userId: string; section?: string }>();
  const navigate = useNavigate();
  const [user, setUser] = useState<AdminUser | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    setUser(null);
    setError(null);
    findAdminUserById(userId)
      .then(found => {
        if (cancelled) return;
        if (found) setUser(found);
        else setError('That user no longer exists.');
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const activeSection: UserDetailSection = isUserDetailSection(section) ? section : 'activity';

  if (error) {
    return (
      <div className="text-sm">
        <p className="text-danger">{error}</p>
        <button
          type="button"
          className="mt-2 text-primary hover:underline"
          onClick={() => navigate(adminPath('users'))}
        >
          Back to Users
        </button>
      </div>
    );
  }

  if (!user) {
    return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;
  }

  return (
    <UserDetailView
      user={user}
      currentUserId={currentUserId}
      section={activeSection}
      onSectionChange={next =>
        navigate(`${adminPath('users')}/${user.id}/${next}`, { replace: true })
      }
      onBackToOverview={() => navigate(adminPath('overview'))}
      onBackToUsers={() => navigate(adminPath('users'))}
    />
  );
}

// ---------------------------------------------------------------------------
// Main admin panel
// ---------------------------------------------------------------------------

export default function AdminPanel() {
  useAutoReloadOnDeploy();

  const { pathname, hash } = useLocation();
  const legacyTarget = legacyHashTarget(pathname, hash);

  const { currentAuthUser } = useAuth();
  const currentUserId = currentAuthUser ? String(currentAuthUser.id) : undefined;

  const navigate = useNavigate();
  const goToSection = useCallback((slug: string) => navigate(adminPath(slug)), [navigate]);

  if (legacyTarget) return <Navigate to={legacyTarget} replace />;

  return (
    <Routes>
      <Route index element={<Navigate to="overview" replace />} />
      <Route path="overview" element={<OverviewRoute />} />
      <Route path="users" element={<UsersRoute currentUserId={currentUserId} />} />
      <Route path="users/:userId" element={<UserDetailRoute currentUserId={currentUserId} />} />
      <Route
        path="users/:userId/:section"
        element={<UserDetailRoute currentUserId={currentUserId} />}
      />
      <Route
        path="access"
        element={
          <AdminSection slug="access">
            <AccessAndWaitlistTab />
          </AdminSection>
        }
      />
      <Route
        path="reported"
        element={
          <AdminSection slug="reported">
            <ReportedTab onSelectUserById={userId => navigate(`${adminPath('users')}/${userId}`)} />
          </AdminSection>
        }
      />
      <Route
        path="monitoring"
        element={
          <AdminSection slug="monitoring">
            <MonitoringTab />
          </AdminSection>
        }
      />
      <Route
        path="model-eval"
        element={
          <AdminSection slug="model-eval">
            <ModelEvalTab />
          </AdminSection>
        }
      />
      <Route
        path="config"
        element={
          <AdminSection slug="config">
            <ConfigTab />
          </AdminSection>
        }
      />
      <Route
        path="api-keys"
        element={
          <AdminSection slug="api-keys">
            <ApiKeysTab />
          </AdminSection>
        }
      />
      <Route path="*" element={<UnknownSection onGoToOverview={() => goToSection('overview')} />} />
    </Routes>
  );
}

function UnknownSection({ onGoToOverview }: { onGoToOverview: () => void }) {
  return (
    <div className="text-sm">
      <p className="text-muted-foreground">That admin page does not exist.</p>
      <button type="button" className="mt-2 text-primary hover:underline" onClick={onGoToOverview}>
        Go to Overview
      </button>
    </div>
  );
}
