import type {
  AuthConfig,
  AuthUser,
  ChatAccepted,
  ChatResponse,
  UserProfileResponse,
  UserProfileUpdate,
  MemoryResponse,
  MemoryUpdate,
  ChannelConfigResponse,
  ChannelConfigUpdate,
  ChannelRouteListResponse,
  ChannelRouteResponse,
  ModelConfigResponse,
  ModelConfigUpdate,
  StorageConfigResponse,
  StorageConfigUpdate,
  OAuthAuthorizeResponse,
  OAuthStatusResponse,
  ProviderInfo,
  SessionDetailResponse,
  SessionListResponse,
  ToolConfigResponse,
  ToolConfigUpdateEntry,
} from '@/types';
import client, { getAccessToken, setAccessToken, setRefreshToken } from '@/lib/api-client';
import { tryRestoreSession as _tryRestoreSession } from '@/extensions';

// --- Shared helpers ---

function _getAuthHeaders(): Record<string, string> {
  const token = getAccessToken();
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

/** Throw a typed Error from an openapi-fetch error body. */
function _throwApiError(error: unknown, fallback: string): never {
  const b = error as { detail?: string };
  throw new Error(b.detail || fallback);
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
  getProfile: async () => {
    const { data, error } = await client.GET('/api/user/profile');
    if (error) _throwApiError(error, 'Failed to get profile');
    return data as UserProfileResponse;
  },
  updateProfile: async (body: UserProfileUpdate) => {
    const { data, error } = await client.PUT('/api/user/profile', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update profile');
    return data as UserProfileResponse;
  },

  // Sessions
  getSession: async (sessionId: string) => {
    const { data, error } = await client.GET('/api/user/sessions/{session_id}', {
      params: { path: { session_id: sessionId } },
    });
    if (error) _throwApiError(error, 'Failed to get session');
    return data as SessionDetailResponse;
  },
  listSessions: async (params?: { limit?: number; offset?: number; is_active?: boolean }) => {
    const { data, error } = await client.GET('/api/user/sessions', {
      params: { query: params },
    });
    if (error) _throwApiError(error, 'Failed to list sessions');
    return data as SessionListResponse;
  },

  deleteConversationHistory: async (sessionId: string) => {
    const { error } = await client.DELETE('/api/user/sessions/{session_id}/messages', {
      params: { path: { session_id: sessionId } },
    });
    if (error) _throwApiError(error, 'Failed to delete conversation history');
  },

  // Memory
  getMemory: async () => {
    const { data, error } = await client.GET('/api/user/memory');
    if (error) _throwApiError(error, 'Failed to get memory');
    return data as MemoryResponse;
  },
  updateMemory: async (body: MemoryUpdate) => {
    const { data, error } = await client.PUT('/api/user/memory', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update memory');
    return data as MemoryResponse;
  },

  // Channel config
  getChannelConfig: async () => {
    const { data, error } = await client.GET('/api/user/channels/config');
    if (error) _throwApiError(error, 'Failed to get channel config');
    return data as ChannelConfigResponse;
  },
  updateChannelConfig: async (body: ChannelConfigUpdate) => {
    const { data, error } = await client.PUT('/api/user/channels/config', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update channel config');
    return data as ChannelConfigResponse;
  },

  // Channel routes
  getChannelRoutes: async () => {
    const { data, error } = await client.GET('/api/user/channels/routes');
    if (error) _throwApiError(error, 'Failed to get channel routes');
    return data as ChannelRouteListResponse;
  },
  toggleChannelRoute: async (channel: string, enabled: boolean) => {
    const { data, error } = await client.PATCH('/api/user/channels/routes/{channel}', {
      params: { path: { channel } },
      body: { enabled } as never,
    });
    if (error) _throwApiError(error, 'Failed to toggle channel route');
    return data as ChannelRouteResponse;
  },

  // Model config
  getModelConfig: async () => {
    const { data, error } = await client.GET('/api/user/model/config');
    if (error) _throwApiError(error, 'Failed to get model config');
    return data as ModelConfigResponse;
  },
  updateModelConfig: async (body: ModelConfigUpdate) => {
    const { data, error } = await client.PUT('/api/user/model/config', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update model config');
    return data as ModelConfigResponse;
  },

  // Storage config
  getStorageConfig: async () => {
    const { data, error } = await client.GET('/api/user/storage/config');
    if (error) _throwApiError(error, 'Failed to get storage config');
    return data as StorageConfigResponse;
  },
  updateStorageConfig: async (body: StorageConfigUpdate) => {
    const { data, error } = await client.PUT('/api/user/storage/config', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update storage config');
    return data as StorageConfigResponse;
  },

  // Providers & models
  listProviders: async () => {
    const { data, error } = await client.GET('/api/user/providers');
    if (error) _throwApiError(error, 'Failed to list providers');
    return data as ProviderInfo[];
  },
  listProviderModels: async (provider: string, apiBase?: string) => {
    const { data, error } = await client.GET('/api/user/providers/{provider}/models', {
      params: { path: { provider }, query: { api_base: apiBase } },
    });
    if (error) _throwApiError(error, 'Failed to list provider models');
    return data as string[];
  },

  // Tool config
  getToolConfig: async () => {
    const { data, error } = await client.GET('/api/user/tools');
    if (error) _throwApiError(error, 'Failed to get tool config');
    return data as ToolConfigResponse;
  },
  updateToolConfig: async (tools: ToolConfigUpdateEntry[]) => {
    const { data, error } = await client.PUT('/api/user/tools', {
      body: { tools } as never,
    });
    if (error) _throwApiError(error, 'Failed to update tool config');
    return data as ToolConfigResponse;
  },

  // OAuth
  getOAuthStatus: async () => {
    const { data, error } = await client.GET('/api/oauth/status');
    if (error) _throwApiError(error, 'Failed to get OAuth status');
    return data as OAuthStatusResponse;
  },
  getOAuthAuthorizeUrl: async (integration: string) => {
    const { data, error } = await client.GET('/api/oauth/{integration}/authorize', {
      params: { path: { integration } },
    });
    if (error) _throwApiError(error, 'Failed to get OAuth authorize URL');
    return data as OAuthAuthorizeResponse;
  },
  disconnectOAuth: async (integration: string) => {
    const { error } = await client.DELETE('/api/oauth/{integration}', {
      params: { path: { integration } },
    });
    if (error) _throwApiError(error, 'Failed to disconnect OAuth');
  },

  // Premium channel linking (raw fetch -- these endpoints are premium-only,
  // not in the OSS OpenAPI spec)
  getTelegramLink: async () => {
    const res = await fetch('/api/channels/telegram', { headers: _getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch Telegram link');
    return res.json() as Promise<{ telegram_user_id: string | null; connected: boolean }>;
  },
  getTelegramBotInfo: async () => {
    const res = await fetch('/api/channels/telegram/bot-info', { headers: _getAuthHeaders() });
    if (!res.ok) return null;
    return res.json() as Promise<{ bot_username: string; bot_link: string }>;
  },
  setTelegramLink: async (telegramUserId: string) => {
    const res = await fetch('/api/channels/telegram', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._getAuthHeaders() },
      body: JSON.stringify({ telegram_user_id: telegramUserId }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { detail?: string };
      throw new Error(body.detail || `Failed to save: ${res.status}`);
    }
    return res.json() as Promise<{ telegram_user_id: string | null; connected: boolean }>;
  },
  getLinqLink: async () => {
    const res = await fetch('/api/channels/linq', { headers: _getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch Linq link');
    return res.json() as Promise<{ phone_number: string | null; connected: boolean; linq_from_number?: string }>;
  },
  setLinqLink: async (phoneNumber: string) => {
    const res = await fetch('/api/channels/linq', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._getAuthHeaders() },
      body: JSON.stringify({ phone_number: phoneNumber }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { detail?: string };
      throw new Error(body.detail || `Failed to save: ${res.status}`);
    }
    return res.json() as Promise<{ phone_number: string | null; connected: boolean; linq_from_number?: string }>;
  },

  // Activity stream: real-time agent status from any channel
  subscribeToActivity: (
    onEvent: (event: { type: string; tool_name?: string; channel?: string }) => void,
  ): AbortController => {
    const controller = new AbortController();
    const token = getAccessToken();

    fetch('/api/user/chat/activity', {
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      signal: controller.signal,
    })
      .then((res) => {
        if (!res.ok || !res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        const read = (): void => {
          reader
            .read()
            .then(({ done, value }) => {
              if (done) return;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() || '';
              for (const line of lines) {
                if (line.startsWith('data: ')) {
                  try {
                    const payload = JSON.parse(line.slice(6)) as {
                      type: string;
                      tool_name?: string;
                      channel?: string;
                    };
                    onEvent(payload);
                  } catch {
                    // skip malformed JSON
                  }
                }
              }
              read();
            })
            .catch(() => {
              // Stream ended or aborted
            });
        };
        read();
      })
      .catch(() => {
        // Connection failed or aborted
      });

    return controller;
  },

  // Chat (async: POST submits, SSE delivers reply -- stays manual)
  sendChatMessage: async (
    message: string,
    sessionId?: string,
    files?: File[],
    onEvent?: (event: { type: string; tool_name?: string; content?: string }) => void,
    onAccepted?: (accepted: ChatAccepted) => void,
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

    // Step 1: Submit message to bus (raw fetch for multipart/form-data)
    const submitRes = await fetch('/api/user/chat', {
      method: 'POST',
      headers: _getAuthHeaders(),
      body: formData,
    });
    if (!submitRes.ok) {
      const body = await submitRes.json().catch(() => ({}));
      const b = body as { detail?: string };
      throw new Error(b.detail || `Request failed: ${submitRes.status}`);
    }
    const accepted = (await submitRes.json()) as ChatAccepted;
    onAccepted?.(accepted);

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
                        type?: string;
                        tool_name?: string;
                        content?: string;
                      };
                      if (payload.error) {
                        reader.cancel();
                        reject(new Error(payload.error));
                        return;
                      }
                      // Forward intermediate events (tool_call, thinking, etc.)
                      if (payload.type && !payload.reply && onEvent) {
                        onEvent({
                          type: payload.type,
                          tool_name: payload.tool_name,
                          content: payload.content,
                        });
                        continue;
                      }
                      if (payload.reply !== undefined) {
                        reader.cancel();
                        resolve({
                          reply: payload.reply || '',
                          session_id: accepted.session_id,
                        });
                        return;
                      }
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
