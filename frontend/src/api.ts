import type {
  AuthConfig,
  AuthUser,
  ChannelConfig,
  ChannelConfigUpdate,
  ChatAccepted,
  ChatResponse,
  ChecklistItem,
  ChecklistItemUpdate,
  ContractorProfile,
  ContractorProfileUpdate,
  ContractorStats,
  MemoryFact,
  MemoryFactUpdate,
  SessionDetail,
  SessionListResponse,
  ToolConfigResponse,
  ToolConfigUpdateEntry,
} from '@/types';
import { getAccessToken, setAccessToken, setRefreshToken } from '@/lib/api-client';
import { tryRestoreSession as _tryRestoreSession } from '@/extensions';

// --- Storage keys ---
const STORAGE_KEYS = {
  REFRESH_TOKEN: 'clawbolt_refresh_token',
  THEME: 'clawbolt_theme',
} as const;

export { STORAGE_KEYS };

// --- Shared helpers ---

function _getAuthHeaders(): Record<string, string> {
  const token = getAccessToken();
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

async function _fetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { ...init?.headers, ..._getAuthHeaders() },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const b = body as { detail?: string };
    throw new Error(b.detail || `Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function _fetchVoid(url: string, init?: RequestInit): Promise<void> {
  const res = await fetch(url, {
    ...init,
    headers: { ...init?.headers, ..._getAuthHeaders() },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const b = body as { detail?: string };
    throw new Error(b.detail || `Request failed: ${res.status}`);
  }
}

// --- Auth API ---

async function getAuthConfig(): Promise<AuthConfig> {
  const res = await fetch('/api/auth/config');
  return res.json() as Promise<AuthConfig>;
}

function logout(): void {
  setAccessToken(null);
  setRefreshToken(null);
}

const api = {
  getAuthConfig,
  logout,
  tryRestoreSession: _tryRestoreSession as () => Promise<AuthUser | null>,

  // Profile
  getProfile: () => _fetch<ContractorProfile>('/api/user/profile'),
  updateProfile: (body: ContractorProfileUpdate) =>
    _fetch<ContractorProfile>('/api/user/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Sessions
  listSessions: (offset = 0, limit = 20) =>
    _fetch<SessionListResponse>(`/api/user/sessions?offset=${offset}&limit=${limit}`),
  getSession: (sessionId: string) =>
    _fetch<SessionDetail>(`/api/user/sessions/${encodeURIComponent(sessionId)}`),

  // Memory
  listMemoryFacts: (category?: string) => {
    const params = category ? `?category=${encodeURIComponent(category)}` : '';
    return _fetch<MemoryFact[]>(`/api/user/memory${params}`);
  },
  updateMemoryFact: (key: string, body: MemoryFactUpdate) =>
    _fetch<MemoryFact>(`/api/user/memory/${encodeURIComponent(key)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteMemoryFact: (key: string) =>
    _fetchVoid(`/api/user/memory/${encodeURIComponent(key)}`, { method: 'DELETE' }),

  // Checklist
  listChecklist: () => _fetch<ChecklistItem[]>('/api/user/checklist'),
  createChecklistItem: (body: { description: string; schedule?: string }) =>
    _fetch<ChecklistItem>('/api/user/checklist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateChecklistItem: (id: number, body: ChecklistItemUpdate) =>
    _fetch<ChecklistItem>(`/api/user/checklist/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteChecklistItem: (id: number) =>
    _fetchVoid(`/api/user/checklist/${id}`, { method: 'DELETE' }),

  // Channel config
  getChannelConfig: () => _fetch<ChannelConfig>('/api/user/channels/config'),
  updateChannelConfig: (body: ChannelConfigUpdate) =>
    _fetch<ChannelConfig>('/api/user/channels/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Tool config
  getToolConfig: () => _fetch<ToolConfigResponse>('/api/user/tools'),
  updateToolConfig: (tools: ToolConfigUpdateEntry[]) =>
    _fetch<ToolConfigResponse>('/api/user/tools', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tools }),
    }),

  // Stats
  getStats: () => _fetch<ContractorStats>('/api/user/stats'),

  // Chat (async: POST submits, SSE delivers reply)
  sendChatMessage: async (
    message: string,
    sessionId?: string,
    files?: File[],
  ): Promise<ChatResponse> => {
    const formData = new FormData();
    formData.append('message', message);
    if (sessionId) {
      formData.append('session_id', sessionId);
    }
    if (files) {
      for (const file of files) {
        formData.append('files', file);
      }
    }

    // Step 1: Submit message to bus
    const accepted = await _fetch<ChatAccepted>('/api/user/chat', {
      method: 'POST',
      body: formData,
    });

    // Step 2: Open SSE connection to receive the reply
    return new Promise<ChatResponse>((resolve, reject) => {
      const token = getAccessToken();
      const url = `/api/user/chat/events/${encodeURIComponent(accepted.request_id)}`;

      // EventSource does not support custom headers, so we use fetch + ReadableStream
      fetch(url, {
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      })
        .then((res) => {
          if (!res.ok) {
            reject(new Error(`SSE request failed: ${res.status}`));
            return;
          }
          const reader = res.body?.getReader();
          if (!reader) {
            reject(new Error('No response body'));
            return;
          }
          const decoder = new TextDecoder();
          let buffer = '';

          const read = (): void => {
            reader
              .read()
              .then(({ done, value }) => {
                if (done) {
                  // Stream ended without data
                  reject(new Error('SSE stream ended without reply'));
                  return;
                }
                buffer += decoder.decode(value, { stream: true });

                // Parse SSE lines
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                  if (line.startsWith('data: ')) {
                    try {
                      const payload = JSON.parse(line.slice(6)) as {
                        reply?: string;
                        error?: string;
                      };
                      if (payload.error) {
                        reader.cancel();
                        reject(new Error(payload.error));
                        return;
                      }
                      reader.cancel();
                      resolve({
                        reply: payload.reply || '',
                        session_id: accepted.session_id,
                      });
                      return;
                    } catch {
                      // Continue reading if JSON parse fails
                    }
                  }
                }
                read();
              })
              .catch(reject);
          };
          read();
        })
        .catch(reject);
    });
  },
};

export default api;
