import type { Page } from '@playwright/test';
import { expect } from '@playwright/test';

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
 * Complete onboarding by marking onboarding_complete=true via the API.
 * This avoids the get-started redirect so tests can go straight to /app/dashboard.
 */
export async function completeOnboarding(baseUrl: string): Promise<void> {
  const res = await fetch(`${baseUrl}/api/user/profile`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ onboarding_complete: true }),
  });
  if (!res.ok) {
    throw new Error(`Failed to complete onboarding: ${res.status} ${await res.text()}`);
  }
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
