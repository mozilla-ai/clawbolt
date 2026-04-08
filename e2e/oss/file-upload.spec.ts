import { test, expect } from '@playwright/test';
import {
  waitForAppReady,
  completeOnboarding,
  createTestPng,
} from '../fixtures/test-helpers';

test.describe('File Upload - API Tests', () => {
  test('POST /api/user/chat with message and file returns 200', async ({
    request,
    baseURL,
  }) => {
    const png = createTestPng();

    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: 'Check this image',
        files: {
          name: 'test-image.png',
          mimeType: 'image/png',
          buffer: png,
        },
      },
    });
    expect(res.ok()).toBe(true);

    const body = await res.json();
    expect(body.request_id).toBeTruthy();
    expect(body.session_id).toBeTruthy();
    expect(typeof body.request_id).toBe('string');
    expect(typeof body.session_id).toBe('string');
  });

  test('POST /api/user/chat with only a file (no message) returns 200', async ({
    request,
    baseURL,
  }) => {
    const png = createTestPng();

    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: '',
        files: {
          name: 'photo.png',
          mimeType: 'image/png',
          buffer: png,
        },
      },
    });
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.request_id).toBeTruthy();
  });

  test('POST /api/user/chat with no message and no files returns 422', async ({
    request,
    baseURL,
  }) => {
    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: '',
      },
    });
    expect(res.status()).toBe(422);
    const body = await res.json();
    expect(body.detail).toContain('Either message text or files required');
  });

  test('POST /api/user/chat with oversized file returns 422', async ({
    request,
    baseURL,
  }) => {
    // max_media_size_bytes defaults to 20_971_520 (20 MB)
    const oversized = Buffer.alloc(20_971_521, 0);

    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: 'big file',
        files: {
          name: 'huge.bin',
          mimeType: 'application/octet-stream',
          buffer: oversized,
        },
      },
    });
    expect(res.status()).toBe(422);
    const body = await res.json();
    expect(body.detail).toContain('File too large');
  });

  test('POST /api/user/chat with message only (no files) returns 200', async ({
    request,
    baseURL,
  }) => {
    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: 'Hello assistant',
      },
    });
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.request_id).toBeTruthy();
    expect(body.session_id).toBeTruthy();
  });

  test('POST /api/user/chat with invalid session_id returns 422', async ({
    request,
    baseURL,
  }) => {
    const res = await request.post(`${baseURL}/api/user/chat`, {
      multipart: {
        message: 'test',
        session_id: 'invalid-format',
      },
    });
    expect(res.status()).toBe(422);
    const body = await res.json();
    expect(body.detail).toContain('session_id must match pattern');
  });
});

test.describe('File Upload - UI Tests', () => {
  test.beforeEach(async ({ baseURL }) => {
    await completeOnboarding(baseURL!);
  });

  test('chat page loads with attach button visible', async ({ page }) => {
    await page.goto('/app/chat');
    await expect(page.getByPlaceholder('Type a message...')).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByLabel('Attach files')).toBeVisible();
  });

  test('selecting a file shows preview chip', async ({ page }) => {
    await page.goto('/app/chat');
    await expect(page.getByPlaceholder('Type a message...')).toBeVisible({
      timeout: 10_000,
    });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: 'test-photo.png',
      mimeType: 'image/png',
      buffer: createTestPng(),
    });

    await expect(page.getByText('test-photo.png')).toBeVisible();
  });

  test('selecting multiple files shows multiple preview chips', async ({
    page,
  }) => {
    await page.goto('/app/chat');
    await expect(page.getByPlaceholder('Type a message...')).toBeVisible({
      timeout: 10_000,
    });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles([
      { name: 'photo1.png', mimeType: 'image/png', buffer: createTestPng() },
      { name: 'photo2.png', mimeType: 'image/png', buffer: createTestPng() },
    ]);

    await expect(page.getByText('photo1.png')).toBeVisible();
    await expect(page.getByText('photo2.png')).toBeVisible();
  });

  test('removing a file attachment works', async ({ page }) => {
    await page.goto('/app/chat');
    await expect(page.getByPlaceholder('Type a message...')).toBeVisible({
      timeout: 10_000,
    });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: 'removable.png',
      mimeType: 'image/png',
      buffer: createTestPng(),
    });

    await expect(page.getByText('removable.png')).toBeVisible();

    await page.getByLabel('Remove removable.png').click();

    await expect(page.getByText('removable.png')).not.toBeVisible();
  });

  test('image file shows thumbnail preview', async ({ page }) => {
    await page.goto('/app/chat');
    await expect(page.getByPlaceholder('Type a message...')).toBeVisible({
      timeout: 10_000,
    });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: 'thumb-test.png',
      mimeType: 'image/png',
      buffer: createTestPng(),
    });

    const thumbnail = page.locator('img[alt="thumb-test.png"]');
    await expect(thumbnail).toBeVisible();
  });
});
