import { test, expect } from '@playwright/test';

test.describe('Storage Config - API Tests', () => {
  test('GET /api/user/storage/config returns current config', async ({
    request,
    baseURL,
  }) => {
    const res = await request.get(`${baseURL}/api/user/storage/config`);
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.storage_provider).toBe('local');
    expect(body.file_storage_base_dir).toBeTruthy();
    expect(typeof body.dropbox_access_token_set).toBe('boolean');
    expect(typeof body.google_drive_credentials_json_set).toBe('boolean');
  });

  test('PUT /api/user/storage/config updates provider to local', async ({
    request,
    baseURL,
  }) => {
    const res = await request.put(`${baseURL}/api/user/storage/config`, {
      data: { storage_provider: 'local' },
    });
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.storage_provider).toBe('local');
  });

  test('PUT /api/user/storage/config rejects invalid provider', async ({
    request,
    baseURL,
  }) => {
    const res = await request.put(`${baseURL}/api/user/storage/config`, {
      data: { storage_provider: 'invalid_provider' },
    });
    expect(res.status()).toBe(422);
    const body = await res.json();
    expect(body.detail).toContain('Invalid storage_provider');
  });

  test('PUT /api/user/storage/config with empty body returns 400', async ({
    request,
    baseURL,
  }) => {
    const res = await request.put(`${baseURL}/api/user/storage/config`, {
      data: {},
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.detail).toContain('No fields to update');
  });
});
