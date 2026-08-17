import { test, expect } from '@playwright/test';
import { collectConsoleErrors } from '../fixtures/test-helpers';

// The security-headers middleware only mounts under multi_user, so these
// assertions belong to this project rather than the OSS one.
test.describe('Content Security Policy', () => {
  test('homepage loads without CSP violations', async ({ page }) => {
    const { cspViolations } = await collectConsoleErrors(page, async () => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');
    });

    expect(cspViolations).toEqual([]);
  });

  test('login page loads without CSP violations', async ({ page }) => {
    const { cspViolations } = await collectConsoleErrors(page, async () => {
      await page.goto('/app/login');
      await page.waitForLoadState('networkidle');
    });

    expect(cspViolations).toEqual([]);
  });

  test('CSP header is present on HTML responses', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/`);
    const csp = res.headers()['content-security-policy'];
    expect(csp).toBeTruthy();
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("style-src 'self' 'unsafe-inline'");
    expect(csp).toContain("img-src 'self' data: blob:");
  });

  test('CSP header includes frame-ancestors none', async ({ request, baseURL }) => {
    const res = await request.get(`${baseURL}/`);
    const csp = res.headers()['content-security-policy'];
    expect(csp).toContain("frame-ancestors 'none'");
  });
});
