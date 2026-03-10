import type { KnipConfig } from 'knip';

const config: KnipConfig = {
  entry: [
    'src/hero.ts', // CSS @plugin import (not detectable by knip)
    'src/extensions/index.ts', // Public API barrel for premium overlay
    'src/extensions/admin/index.tsx', // OSS stub replaced by premium overlay
    'src/extensions/admin/admin-api.ts', // OSS stub replaced by premium overlay
    'src/lib/api-client.ts', // Public API for premium overlay
  ],
  project: ['src/**/*.{ts,tsx}'],
  ignore: ['src/generated/**'], // Created by generate:api script
  vite: { config: ['vite.config.ts'] },
};

export default config;
