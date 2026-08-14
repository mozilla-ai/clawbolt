export interface NavItem {
  label: string;
  slug: string;
}

export interface NavSection {
  label: string;
  items: NavItem[];
}

export const docsNav: NavSection[] = [
  {
    label: 'User Guide',
    items: [
      { label: 'What is Clawbolt?', slug: 'guide' },
      { label: 'First Steps', slug: 'guide/getting-started' },
      { label: 'Memory', slug: 'guide/memory' },
      { label: 'Photos & Files', slug: 'guide/photos' },
      { label: 'Estimates', slug: 'guide/estimates' },
      { label: 'Calendar', slug: 'guide/calendar' },
      { label: 'Heartbeat', slug: 'guide/heartbeat' },
      { label: 'Integrations', slug: 'guide/integrations' },
      { label: 'Dashboard', slug: 'guide/dashboard' },
      { label: 'Tips & Tricks', slug: 'guide/tips' },
    ],
  },
  {
    label: 'Features',
    items: [
      { label: 'Memory', slug: 'features/memory' },
      { label: 'Photos', slug: 'features/photos' },
      { label: 'File Cataloging', slug: 'features/file-cataloging' },
      { label: 'Heartbeat', slug: 'features/heartbeat' },
      { label: 'Google Calendar', slug: 'features/calendar' },
      { label: 'QuickBooks Online', slug: 'features/quickbooks' },
    ],
  },
];

export function findNavItem(slug: string): NavItem | null {
  for (const section of docsNav) {
    for (const item of section.items) {
      if (item.slug === slug) return item;
    }
  }
  return null;
}
