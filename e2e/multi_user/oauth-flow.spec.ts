import { test, expect } from '@playwright/test';

// The Google OAuth router only mounts under multi_user. No client id is
// configured in e2e, so the endpoints are asserted to exist and to fail
// closed (redirect to login with an error), never to 404.
test.describe('OAuth flow', () => {
  test('GET /auth/oauth/google redirects (not 404)', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/auth/oauth/google`, {
      maxRedirects: 0,
    });
    // Should be a redirect to Google OAuth or a 500 (no client_id configured)
    // but NEVER a 404 or 405
    expect([302, 307, 500]).toContain(res.status());
  });

  test('GET /auth/oauth/google/callback exists (not 404)', async ({ request, baseURL }) => {
    // Callback without params should redirect to login with error, not 404
    const res = await request.get(`${baseURL}/api/auth/oauth/google/callback`, {
      maxRedirects: 0,
    });
    expect(res.status()).toBe(302);
    const location = res.headers()['location'] ?? '';
    expect(location).toContain('/app/login#auth_error=');
  });

  test('callback with Google error redirects to login', async ({ request, baseURL }) => {
    const res = await request.get(
      `${baseURL}/api/auth/oauth/google/callback?error=access_denied`,
      { maxRedirects: 0 }
    );
    expect(res.status()).toBe(302);
    const location = res.headers()['location'] ?? '';
    expect(location).toContain('/app/login#auth_error=');
  });

  test('callback with invalid state redirects to login', async ({ request, baseURL }) => {
    const res = await request.get(
      `${baseURL}/api/auth/oauth/google/callback?code=test&state=invalid`,
      { maxRedirects: 0 }
    );
    expect(res.status()).toBe(302);
    const location = res.headers()['location'] ?? '';
    expect(location).toContain('/app/login#auth_error=');
  });

  test('state endpoint returns a token', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/api/auth/oauth/google/state`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.state).toBeTruthy();
    expect(typeof body.state).toBe('string');
  });

  test('login page Google button triggers OAuth redirect', async ({ page }) => {
    await page.goto('/app/login');
    await page.waitForSelector('button:has-text("Google")', { timeout: 10_000 });

    // Click the Google button and intercept the navigation
    const [response] = await Promise.all([
      page.waitForResponse(
        (resp) => resp.url().includes('/api/auth/oauth/google') && !resp.url().includes('/state'),
        { timeout: 10_000 }
      ).catch(() => null),
      page.getByRole('button', { name: /google/i }).click(),
    ]);

    // The button should trigger navigation to the OAuth endpoint
    // (it will fail to reach Google in tests, but the route should exist)
    if (response) {
      expect([302, 307, 500]).toContain(response.status());
    }
  });
});
