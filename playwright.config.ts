import { defineConfig, devices } from '@playwright/test';

const OSS_PORT = 8765;
const MULTI_USER_PORT = 8766;

const OSS_DATABASE_URL =
  process.env.DATABASE_URL ??
  'postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_e2e';

// The two servers run side by side, so they cannot share a database. The OSS
// suite owns the single user row and rewrites it (see completeOnboarding in
// the fixtures), which is the state the multi_user suite must not observe.
// start-server.sh creates whichever database the URL names.
const MULTI_USER_DATABASE_URL = `${OSS_DATABASE_URL}_multi_user`;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['html', { open: 'never' }]],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  projects: [
    {
      name: 'oss',
      testDir: './e2e/oss',
      use: {
        baseURL: `http://127.0.0.1:${OSS_PORT}`,
      },
    },
    {
      name: 'multi_user',
      testDir: './e2e/multi_user',
      use: {
        baseURL: `http://127.0.0.1:${MULTI_USER_PORT}`,
      },
    },
  ],
  webServer: [
    {
      command: `bash e2e/scripts/start-server.sh ${OSS_PORT}`,
      port: OSS_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        DATABASE_URL: OSS_DATABASE_URL,
      },
    },
    {
      // Same entrypoint, opposite tenancy. AUTH_MODE is read once when
      // backend.app.config is imported, so the mode has to arrive as
      // environment rather than being flipped after boot.
      command: `bash e2e/scripts/start-server.sh ${MULTI_USER_PORT}`,
      port: MULTI_USER_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        AUTH_MODE: 'multi_user',
        DATABASE_URL: MULTI_USER_DATABASE_URL,
      },
    },
  ],
});
