import { test, expect } from '@playwright/test';
import { waitForHomepage, waitForLoginPage } from '../fixtures/test-helpers';

test.describe('Multi-user smoke tests', () => {
  test('health endpoint returns ok', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/health`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.status).toBe('ok');
  });

  test('auth config returns oauth_google with required true', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/auth/config`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.method).toBe('oauth_google');
    expect(body.required).toBe(true);
  });

  test('homepage renders at / (not redirected to /app)', async ({ page }) => {
    await page.goto('/');
    await waitForHomepage(page);

    // Should be on the marketing homepage, not /app
    expect(page.url()).not.toContain('/app');

    // Logo should be visible on homepage
    const logo = page.locator('img[src*="clawbolt"]');
    await expect(logo.first()).toBeVisible();
  });

  test('homepage has Clawbolt branding in header', async ({ page }) => {
    await page.goto('/');
    await waitForHomepage(page);

    // Header should have the Clawbolt name
    await expect(page.getByRole('link', { name: /clawbolt/i })).toBeVisible();
  });

  test('login page renders at /app/login', async ({ page }) => {
    await page.goto('/app/login');
    await waitForLoginPage(page);

    // Should show a Google sign-in button
    await expect(page.getByRole('button', { name: /google/i })).toBeVisible();
  });

  test('unauthenticated /app redirects to login', async ({ page }) => {
    await page.goto('/app');
    // Should end up on the login page (no valid session)
    await page.waitForURL('**/app/login**', { timeout: 10_000 });
  });
});
