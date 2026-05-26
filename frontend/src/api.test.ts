import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { _authedFetch } from './api';
import { setAccessToken, setRefreshToken } from './lib/api-client';

function makeJwt(exp: number): string {
  const b64 = (s: string): string =>
    btoa(s).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${b64(JSON.stringify({ exp, sub: 'test-user' }))}.signature`;
}

describe('_authedFetch', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const NOW_S = Math.floor(Date.now() / 1000);

  beforeEach(() => {
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    localStorage.setItem('clawbolt_refresh_token', 'rt-test');
  });

  afterEach(() => {
    setAccessToken(null);
    setRefreshToken(null);
    vi.restoreAllMocks();
  });

  it('attaches the current bearer token to outgoing requests', async () => {
    setAccessToken(makeJwt(NOW_S + 3600));
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 200 }));

    await _authedFetch('/api/whatever', { method: 'POST' });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toMatch(/^Bearer eyJ/);
  });

  it('proactively refreshes when the access token is within the leeway window', async () => {
    // Token expires in 10s, leeway is 30s, so refresh should fire BEFORE the
    // actual request goes out.
    setAccessToken(makeJwt(NOW_S + 10));

    // Refresh response first, then the actual request.
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: makeJwt(NOW_S + 3600),
            refresh_token: 'rt-new',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(new Response('ok', { status: 200 }));

    await _authedFetch('/api/user/chat', { method: 'POST', body: new FormData() });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/api/auth/refresh');
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/user/chat');
    // Second call carries the fresh token.
    const retryInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect((retryInit.headers as Record<string, string>).Authorization).toMatch(/^Bearer /);
  });

  it('does not refresh proactively when the token has plenty of life left', async () => {
    setAccessToken(makeJwt(NOW_S + 3600));
    fetchMock.mockResolvedValueOnce(new Response('ok', { status: 200 }));

    await _authedFetch('/api/user/chat', { method: 'POST' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/user/chat');
  });

  it('refreshes once and retries on a 401 response', async () => {
    setAccessToken(makeJwt(NOW_S + 3600));

    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 401 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: makeJwt(NOW_S + 3600),
            refresh_token: 'rt-new',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(new Response('ok', { status: 200 }));

    const res = await _authedFetch('/api/user/chat', { method: 'POST' });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // Original, refresh, retry.
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/user/chat');
    expect(fetchMock.mock.calls[1]?.[0]).toContain('/api/auth/refresh');
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/user/chat');
  });

  it('returns the 401 response without retry when refresh fails', async () => {
    setAccessToken(makeJwt(NOW_S + 3600));

    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 401 }))
      .mockResolvedValueOnce(new Response('', { status: 401 }));

    const res = await _authedFetch('/api/user/chat', { method: 'POST' });

    expect(res.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not retry on non-401 failures (e.g. 403)', async () => {
    setAccessToken(makeJwt(NOW_S + 3600));
    fetchMock.mockResolvedValueOnce(new Response('', { status: 403 }));

    const res = await _authedFetch('/api/user/chat', { method: 'POST' });

    expect(res.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('passes init fields through (signal, method, etc.) to fetch', async () => {
    // Long-lived SSE callers rely on signal propagation so abort() actually
    // tears down the connection. Verify init flows through verbatim on both
    // the original request and the retry path.
    setAccessToken(makeJwt(NOW_S + 3600));
    const controller = new AbortController();

    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 401 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: makeJwt(NOW_S + 3600),
            refresh_token: 'rt-new',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(new Response('ok', { status: 200 }));

    await _authedFetch('/api/user/chat/activity', { signal: controller.signal });

    const firstInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const retryInit = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect(firstInit.signal).toBe(controller.signal);
    expect(retryInit.signal).toBe(controller.signal);
  });
});

