import { useState, type ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import ThemeToggle from '@/components/ui/ThemeToggle';
import { getFeatureRequestUrl, getReportIssueUrl, getDocsUrl } from './routes';

export interface SidebarFooterProps {
  isPremium: boolean;
  handleLogout: () => void;
  closeSidebar: () => void;
}

/**
 * Collapsible "More" bar: theme, Get Started, help links, and, on a
 * multi-user deployment, the legal pages and Log out.
 */
export function renderSidebarFooter({
  isPremium,
  handleLogout,
  closeSidebar,
}: SidebarFooterProps): ReactNode {
  return (
    <ExpandableFooter
      isPremium={isPremium}
      handleLogout={handleLogout}
      closeSidebar={closeSidebar}
    />
  );
}

function ExpandableFooter({
  isPremium,
  handleLogout,
  closeSidebar,
}: SidebarFooterProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={() => setOpen(prev => !prev)}
        className="flex items-center justify-between w-full px-3 py-2 rounded-md text-xs text-muted-foreground can-hover:hover:text-foreground can-hover:hover:bg-secondary-hover transition-all duration-150"
        aria-expanded={open}
        aria-label="Toggle footer menu"
      >
        <span className="flex items-center gap-2">
          <svg className="w-3.5 h-3.5 shrink-0" fill="currentColor" viewBox="0 0 24 24">
            <circle cx="12" cy="5" r="2" />
            <circle cx="12" cy="12" r="2" />
            <circle cx="12" cy="19" r="2" />
          </svg>
          More
        </span>
        <svg
          className={`w-3 h-3 shrink-0 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
        </svg>
      </button>
      {open && (
        <div className="space-y-1 mt-1">
          <div className="flex items-center justify-between px-3 py-1.5 text-xs text-muted-foreground">
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
          {/* A deployment that has not published legal prose renders a
              placeholder at these routes, so only advertise them where
              there is an operator with users to have terms with. */}
          {isPremium && (
            <>
            <a
              href="/terms"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 px-3 py-2 rounded-md text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              Terms of Service
            </a>
            <a
              href="/privacy"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 px-3 py-2 rounded-md text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
              Privacy Notice
            </a>
            </>
          )}
          <div className="flex gap-2 px-3 py-1 flex-wrap">
            <a
              href={getDocsUrl()}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              Help
            </a>
            <a
              href={getReportIssueUrl()}
              className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              Report issue
            </a>
            <a
              href={getFeatureRequestUrl()}
              className="text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              Feature request
            </a>
          </div>
          {isPremium && (
            <button
              onClick={handleLogout}
              className="flex items-center gap-2 w-full px-3 py-2 rounded-md text-xs text-muted-foreground can-hover:hover:text-foreground transition-all duration-150"
            >
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
              Log out
            </button>
          )}
        </div>
      )}
    </>
  );
}
