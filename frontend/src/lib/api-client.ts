import createClient, { type Middleware } from 'openapi-fetch';
import type { paths } from '@/generated/api';

// --- Token state (shared with api.ts via getters/setters) ---
let _accessToken: string | null = null;
// Decoded ``exp`` claim (seconds since epoch) for the current access token.
// Tracked so we can refresh proactively before the next request goes out
// rather than relying on the reactive 401-then-retry path. Null if the
// token has no parseable JWT payload.
let _accessTokenExp: number | null = null;

// Refresh proactively when within this many seconds of expiry. Generous
// enough to absorb modest client/server clock skew without firing too
// often. The reactive 401 path remains as a safety net.
const _PROACTIVE_REFRESH_LEEWAY_SECONDS = 30;

const REFRESH_TOKEN_KEY = 'clawbolt_refresh_token';

// Exported for unit tests; not part of the public auth surface.
export function _decodeJwtExp(token: string): number | null {
  // JWTs are header.payload.signature, base64url-encoded. We only need
  // the payload's `exp` claim. Failures here are expected for non-JWT
  // tokens; callers fall back to the reactive 401 path.
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const segment = parts[1];
  if (!segment) return null;
  try {
    const payload = segment.replace(/-/g, '+').replace(/_/g, '/');
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4);
    const decoded = JSON.parse(atob(padded)) as { exp?: number };
    return typeof decoded.exp === 'number' ? decoded.exp : null;
  } catch {
    return null;
  }
}

// Exported for unit tests. Returns true when the current access token's
// `exp` claim is within ``_PROACTIVE_REFRESH_LEEWAY_SECONDS`` of now.
// A null exp (opaque token / decode failed) returns false: callers fall
// back to the reactive 401-then-retry path.
export function _shouldProactivelyRefresh(
  exp: number | null,
  nowSeconds: number = Date.now() / 1000,
): boolean {
  if (exp === null) return false;
  return nowSeconds >= exp - _PROACTIVE_REFRESH_LEEWAY_SECONDS;
}

// Exported for unit tests. Reflects the exp tracked at the most recent
// setAccessToken call.
export function _getAccessTokenExp(): number | null {
  return _accessTokenExp;
}

// Consume refresh token from URL hash fragment (set by OAuth redirect).
// This runs once at module load, before any auth checks.
(function _consumeHashToken() {
  const match = window.location.hash.match(/refresh_token=([^&]+)/);
  if (match?.[1]) {
    localStorage.setItem(REFRESH_TOKEN_KEY, match[1]);
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
})();

function _getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

function _setRefreshToken(token: string | null): void {
  if (token) {
    localStorage.setItem(REFRESH_TOKEN_KEY, token);
  } else {
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }
}

export function getAccessToken(): string | null {
  return _accessToken;
}

export function setAccessToken(token: string | null): void {
  _accessToken = token;
  _accessTokenExp = token ? _decodeJwtExp(token) : null;
}

export function setRefreshToken(token: string | null): void {
  _setRefreshToken(token);
}

// --- Refresh token deduplication ---
let _refreshPromise: Promise<boolean> | null = null;

async function _doRefresh(): Promise<boolean> {
  const refreshToken = _getRefreshToken();
  if (!refreshToken) return false;

  try {
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      _accessToken = null;
      _setRefreshToken(null);
      return false;
    }
    const data = (await res.json()) as { access_token: string; refresh_token: string };
    _accessToken = data.access_token;
    _setRefreshToken(data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

export async function tryRefresh(): Promise<boolean> {
  if (!_refreshPromise) {
    _refreshPromise = _doRefresh().finally(() => {
      _refreshPromise = null;
    });
  }
  return _refreshPromise;
}

// --- Auth middleware for openapi-fetch ---
const authMiddleware: Middleware = {
  async onRequest({ request }) {
    // Proactive refresh: if the current access token is within
    // _PROACTIVE_REFRESH_LEEWAY_SECONDS of its `exp`, refresh before
    // sending. Eliminates the 401-then-retry chatter that shows up
    // in the network panel on every page load near expiry.
    if (_accessToken && _shouldProactivelyRefresh(_accessTokenExp)) {
      await tryRefresh();
    }
    if (_accessToken) {
      request.headers.set('Authorization', `Bearer ${_accessToken}`);
    }
    return request;
  },
  async onResponse({ request, response }) {
    if (response.status === 401) {
      const refreshed = await tryRefresh();
      if (refreshed) {
        const retryRequest = new Request(request, {
          headers: new Headers(request.headers),
        });
        retryRequest.headers.set('Authorization', `Bearer ${_accessToken}`);
        return fetch(retryRequest);
      }
      window.dispatchEvent(new CustomEvent('clawbolt-logout'));
    }
    return response;
  },
};

// --- Create typed client ---
const client = createClient<paths>({ baseUrl: '' });
client.use(authMiddleware);

export default client;
