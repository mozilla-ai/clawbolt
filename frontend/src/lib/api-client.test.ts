import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  _decodeJwtExp,
  _shouldProactivelyRefresh,
  _getAccessTokenExp,
  setAccessToken,
  setRefreshToken,
  tryRefresh,
} from './api-client';

// Build a fake JWT: header.payload.signature, where payload is { exp }.
function makeJwt(exp: number): string {
  const b64 = (s: string): string =>
    btoa(s).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${b64(JSON.stringify({ exp, sub: 'test-user' }))}.signature`;
}

describe('_decodeJwtExp', () => {
  it('extracts exp from a valid JWT', () => {
    expect(_decodeJwtExp(makeJwt(1234567890))).toBe(1234567890);
  });

  it('returns null for tokens that are not JWT shaped', () => {
    expect(_decodeJwtExp('opaque-token')).toBeNull();
    expect(_decodeJwtExp('only.two')).toBeNull();
  });

  it('returns null when the payload has no exp claim', () => {
    const noExp = `eyJhbGciOiJIUzI1NiJ9.${btoa(JSON.stringify({ sub: 'x' }))
      .replace(/=/g, '')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')}.sig`;
    expect(_decodeJwtExp(noExp)).toBeNull();
  });

  it('returns null when the payload is unparseable', () => {
    expect(_decodeJwtExp('header.@@@notbase64@@@.sig')).toBeNull();
  });
});

describe('_shouldProactivelyRefresh', () => {
  const now = 1_000_000;

  it('refreshes when current time is within the leeway window', () => {
    // 30 second leeway: anything from (exp - 30) to exp triggers refresh.
    expect(_shouldProactivelyRefresh(now + 10, now)).toBe(true); // 10s left
    expect(_shouldProactivelyRefresh(now, now)).toBe(true); // already expired
    expect(_shouldProactivelyRefresh(now - 100, now)).toBe(true); // long expired
  });

  it('does not refresh when the token has plenty of time left', () => {
    expect(_shouldProactivelyRefresh(now + 31, now)).toBe(false);
    expect(_shouldProactivelyRefresh(now + 3600, now)).toBe(false);
  });

  it('does not refresh when exp is null (opaque token)', () => {
    expect(_shouldProactivelyRefresh(null, now)).toBe(false);
  });
});

describe('setAccessToken', () => {
  afterEach(() => {
    setAccessToken(null);
    setRefreshToken(null);
  });

  it('records the JWT exp so the middleware can refresh proactively', () => {
    const exp = Math.floor(Date.now() / 1000) + 600;
    setAccessToken(makeJwt(exp));
    expect(_getAccessTokenExp()).toBe(exp);
  });

  it('clears the tracked exp when the token is cleared', () => {
    setAccessToken(makeJwt(123));
    setAccessToken(null);
    expect(_getAccessTokenExp()).toBeNull();
  });

  it('records null exp for opaque (non-JWT) tokens', () => {
    setAccessToken('opaque-token');
    expect(_getAccessTokenExp()).toBeNull();
  });
});

describe('tryRefresh', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.setItem('clawbolt_refresh_token', 'rt-test');
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    setAccessToken(null);
    setRefreshToken(null);
    vi.restoreAllMocks();
  });

  it('coalesces concurrent calls into a single /api/auth/refresh fetch', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: makeJwt(Math.floor(Date.now() / 1000) + 3600),
          refresh_token: 'rt-new',
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );

    const results = await Promise.all([tryRefresh(), tryRefresh(), tryRefresh()]);

    expect(results).toEqual([true, true, true]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/api/auth/refresh');
  });

  it('returns false and clears tokens when the refresh endpoint fails', async () => {
    fetchMock.mockResolvedValueOnce(new Response('', { status: 401 }));

    const ok = await tryRefresh();

    expect(ok).toBe(false);
    expect(localStorage.getItem('clawbolt_refresh_token')).toBeNull();
  });

  it('returns false when no refresh token is stored', async () => {
    localStorage.removeItem('clawbolt_refresh_token');

    const ok = await tryRefresh();

    expect(ok).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
