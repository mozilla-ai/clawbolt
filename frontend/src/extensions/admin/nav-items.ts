import { createElement, type ComponentType } from 'react';

/**
 * The admin section's sub-pages, in sidebar order.
 *
 * This is the single source of truth for three things that used to drift
 * apart: the sidebar fold under "Admin", the nested routes in
 * ``admin/index.tsx``, and the section heading each page renders. Adding a
 * page means adding one entry here plus one ``<Route>``.
 *
 * ``nav.ts`` cannot be a ``.tsx`` file: the premium frontend is overlaid on
 * top of the OSS tree, and a ``nav.tsx`` alongside OSS's ``nav.ts`` would
 * leave two modules resolving for the same import. Hence ``createElement``
 * rather than JSX for the icons.
 */

export interface AdminSubPage {
  /** Path segment under ``/app/admin``. */
  slug: string;
  label: string;
  /** Sentence shown under the page heading. */
  description: string;
  icon: ComponentType;
}

export const ADMIN_BASE_PATH = '/app/admin';

function icon(d: string): ComponentType {
  return function Icon() {
    return createElement(
      'svg',
      {
        className: 'w-4 h-4 shrink-0',
        fill: 'none',
        stroke: 'currentColor',
        viewBox: '0 0 24 24',
      },
      createElement('path', {
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        strokeWidth: 1.5,
        d,
      }),
    );
  };
}

export const ADMIN_SUB_PAGES: readonly AdminSubPage[] = [
  {
    slug: 'overview',
    label: 'Overview',
    description: 'System health and recent activity from users who share their data.',
    icon: icon('M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6'),
  },
  {
    slug: 'users',
    label: 'Users',
    description: 'Everyone on the platform. Open a user for usage, activity, and controls.',
    icon: icon('M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z'),
  },
  {
    slug: 'access',
    label: 'Access',
    description: 'Waitlist requests and the allowlist that gates sign-up.',
    icon: icon('M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z'),
  },
  {
    slug: 'reported',
    label: 'Reported',
    description: 'Conversations users flagged for review.',
    icon: icon('M3 21v-4m0 0V5a2 2 0 012-2h6.5l1 1H21l-3 6 3 6h-8.5l-1-1H5a2 2 0 00-2 2z'),
  },
  {
    slug: 'monitoring',
    label: 'Monitoring',
    description: 'Dependency probes, alert delivery, and recent incidents.',
    icon: icon('M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z'),
  },
  {
    slug: 'config',
    label: 'Config',
    description: 'Channel credentials and the platform-wide LLM defaults.',
    icon: icon('M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z'),
  },
  {
    slug: 'api-keys',
    label: 'API Keys',
    description: 'Long-lived bearer tokens for CLI and script access.',
    icon: icon('M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z'),
  },
] as const;

export function adminPath(slug: string): string {
  return `${ADMIN_BASE_PATH}/${slug}`;
}

export function findAdminSubPage(slug: string | undefined): AdminSubPage | undefined {
  return ADMIN_SUB_PAGES.find(p => p.slug === slug);
}
