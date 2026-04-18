import type { Page } from '@playwright/test';
import { expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';

/**
 * Wait for the OSS app to be fully loaded.
 * In OSS mode, / redirects to /app, which loads the AppShell with sidebar nav.
 * We wait for the sidebar "Dashboard" link to render as a sign the app is ready.
 */
export async function waitForAppReady(page: Page): Promise<void> {
  await expect(
    page.getByRole('link', { name: /dashboard/i })
  ).toBeVisible({ timeout: 15_000 });
}

/**
 * Navigate to the chat page and wait for it to be ready.
 */
export async function navigateToChat(page: Page): Promise<void> {
  await page.getByRole('link', { name: /chat/i }).click();
  await expect(page.getByPlaceholder('Type a message...')).toBeVisible({ timeout: 10_000 });
}

/**
 * Mark the OSS single-tenant user as onboarded so tests can skip the
 * get-started redirect and exercise the dashboard experience.
 *
 * onboarding_complete is backend-owned (flipped by OnboardingSubscriber
 * when the LLM deletes BOOTSTRAP.md or heuristic evidence appears), so
 * there is no HTTP endpoint to flip it directly. For tests we write the
 * flag to the database the server is already using.
 *
 * The baseUrl parameter is accepted for backward compatibility but unused.
 */
export async function completeOnboarding(baseUrl: string): Promise<void> {
  // Ensure the OSS single-tenant user exists before updating it. GET
  // /api/user/profile triggers get_current_user, which lazily creates
  // the local user on first access.
  const res = await fetch(`${baseUrl}/api/user/profile`);
  if (!res.ok) {
    throw new Error(`Failed to ensure user exists: ${res.status} ${await res.text()}`);
  }

  const dbUrl =
    process.env.DATABASE_URL ??
    'postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_e2e';
  execFileSync(
    'psql',
    [dbUrl, '-v', 'ON_ERROR_STOP=1', '-c', 'UPDATE users SET onboarding_complete = true;'],
    { stdio: 'pipe' },
  );
}

/**
 * Create a small test PNG image as a Buffer for API upload tests.
 * Returns a 1x1 red pixel PNG (67 bytes).
 */
export function createTestPng(): Buffer {
  return Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==',
    'base64'
  );
}
