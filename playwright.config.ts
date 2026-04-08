import { defineConfig, devices } from '@playwright/test';

const OSS_PORT = 8765;

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
  ],
  webServer: [
    {
      command: `bash e2e/scripts/start-server.sh ${OSS_PORT}`,
      port: OSS_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        DATABASE_URL:
          process.env.DATABASE_URL ??
          'postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_e2e',
      },
    },
  ],
});
