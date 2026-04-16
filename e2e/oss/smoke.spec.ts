import { test, expect } from '@playwright/test';
import { waitForAppReady, completeOnboarding } from '../fixtures/test-helpers';

test.describe('OSS Smoke Tests', () => {
  test('health endpoint returns ok', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/health`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.status).toBe('ok');
    expect(body.database).toBe('ok');
  });

  test('auth config returns method none with required false', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/auth/config`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.method).toBe('none');
    expect(body.required).toBe(false);
  });

  test('auto-created user profile exists', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/user/profile`);
    expect(res.ok()).toBe(true);
    const profile = await res.json();
    expect(profile.user_id).toBe('local@clawbolt.local');
    expect(profile.is_active).toBe(true);
  });

  test('app loads and dashboard renders after onboarding', async ({ page, baseURL }) => {
    await completeOnboarding(baseURL!);
    await page.goto('/');
    await waitForAppReady(page);

    // Should have redirected to /app/dashboard
    await page.waitForURL('**/app/dashboard', { timeout: 10_000 });
  });

  test('sidebar navigation links are visible', async ({ page, baseURL }) => {
    await completeOnboarding(baseURL!);
    await page.goto('/app/dashboard');
    await waitForAppReady(page);

    // Primary nav items (visible by default).
    await expect(page.getByRole('link', { name: /chat/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /channels/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /integrations/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /settings/i })).toBeVisible();

    // Knowledge (formerly Memory) sits under the collapsed "Advanced" fold.
    await expect(page.getByRole('link', { name: /knowledge/i })).not.toBeVisible();
    await page.getByRole('button', { name: /advanced/i }).click();
    await expect(page.getByRole('link', { name: /knowledge/i })).toBeVisible();
  });
});
