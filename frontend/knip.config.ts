import type { KnipConfig } from 'knip';

const config: KnipConfig = {
  entry: [
    'src/hero.ts', // CSS @plugin import (not detectable by knip)
    'src/extensions/index.ts', // Public API barrel for premium overlay
    'src/extensions/admin/index.tsx', // OSS stub replaced by premium overlay
    'src/extensions/admin/admin-api.ts', // OSS stub replaced by premium overlay
    'src/lib/api-client.ts', // Public API for premium overlay
    'src/sw.ts', // Service worker entry point (referenced by VitePWA injectManifest config)
  ],
  project: ['src/**/*.{ts,tsx}'],
ignoreDependencies: ['tailwind-merge'], // Peer dep of tailwind-variants (HeroUI)
  vite: { config: ['vite.config.ts'] },
};

export default config;
