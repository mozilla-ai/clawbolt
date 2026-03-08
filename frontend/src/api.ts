import type {
  AuthConfig,
  AuthUser,
  ChannelConfig,
  ChannelConfigUpdate,
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

  // Stats
  getStats: () => _fetch<ContractorStats>('/api/user/stats'),

  // Chat
  sendChatMessage: (message: string, sessionId?: string, files?: File[]) => {
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
    return _fetch<ChatResponse>('/api/user/chat', {
      method: 'POST',
      body: formData,
    });
  },
};

export default api;
