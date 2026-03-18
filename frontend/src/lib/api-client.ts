import createClient, { type Middleware } from 'openapi-fetch';

// --- Token state (shared with api.ts via getters/setters) ---
let _accessToken: string | null = null;

const REFRESH_TOKEN_KEY = 'clawbolt_refresh_token';

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
// Using `never` for paths since we don't have generated types yet.
// Once `npm run generate:api` is run against the live server, replace with `paths`.
const client = createClient<Record<string, never>>({ baseUrl: '' });
client.use(authMiddleware);

export default client;
