import type {
  AppConfigResponse,
  AuthConfig,
  AuthUser,
  ChatAccepted,
  ChatResponse,
  UserProfileResponse,
  UserProfileUpdate,
  DataSharingConsentRequest,
  DataSharingConsentResponse,
  MemoryResponse,
  MemoryUpdate,
  PermissionsResponse,
  PermissionsUpdate,
  ChannelConfigResponse,
  ChannelConfigUpdate,
  ChannelRouteListResponse,
  ChannelRouteResponse,
  ModelConfigResponse,
  ModelConfigUpdate,
  OAuthAuthorizeResponse,
  OAuthStatusResponse,
  ProviderInfo,
  SessionDetailResponse,
  SessionSystemPromptResponse,
  ToolConfigResponse,
  ToolConfigUpdateEntry,
} from '@/types';
import client, {
  getAccessToken,
  setAccessToken,
  setRefreshToken,
} from '@/lib/api-client';
import { tryRestoreSession as _tryRestoreSession } from '@/extensions';

// --- Shared helpers ---

function _getAuthHeaders(): Record<string, string> {
  const token = getAccessToken();
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

/**
 * XHR-based upload helper. fetch() with FormData cannot report upload-side
 * progress; for the chat POST the user wants to watch their image bytes go
 * up, so this routes through XMLHttpRequest where ``xhr.upload.onprogress``
 * is available. Honours an AbortSignal and a timeout. Returns a real
 * Response so the existing call site can call ``.json()`` and ``.status``
 * without knowing it's XHR underneath. #1368.
 */
interface UploadOptions {
  signal?: AbortSignal;
  onProgress?: (loaded: number, total: number) => void;
  timeoutMs?: number;
}

function _uploadFormData(
  url: string,
  formData: FormData,
  options: UploadOptions = {},
): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    const headers = _getAuthHeaders();
    for (const [name, value] of Object.entries(headers)) {
      xhr.setRequestHeader(name, value);
    }
    if (options.timeoutMs) {
      xhr.timeout = options.timeoutMs;
    }
    if (options.onProgress) {
      xhr.upload.addEventListener('progress', (e: ProgressEvent) => {
        if (e.lengthComputable) {
          options.onProgress!(e.loaded, e.total);
        }
      });
    }
    xhr.addEventListener('load', () => {
      resolve(new Response(xhr.responseText, { status: xhr.status }));
    });
    xhr.addEventListener('error', () => {
      reject(new TypeError('Network error'));
    });
    xhr.addEventListener('abort', () => {
      reject(new DOMException('Upload aborted', 'AbortError'));
    });
    xhr.addEventListener('timeout', () => {
      reject(new DOMException('Upload timed out', 'TimeoutError'));
    });
    if (options.signal) {
      if (options.signal.aborted) {
        xhr.abort();
        return;
      }
      options.signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    xhr.send(formData);
  });
}

/** Error with an HTTP status attached, so callers can distinguish 404 from other failures. */
class ApiError extends Error {
  status: number | undefined;
  constructor(message: string, status: number | undefined) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Throw a typed Error from an openapi-fetch error body. */
function _throwApiError(error: unknown, fallback: string, status?: number): never {
  const b = error as { detail?: string };
  throw new ApiError(b.detail || fallback, status);
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
  getAppConfig: async (): Promise<AppConfigResponse> => {
    const { data, error } = await client.GET('/api/app/config');
    if (error) _throwApiError(error, 'Failed to get app config');
    return data as AppConfigResponse;
  },
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

  // Data sharing consent (kept off the generic profile PUT so every
  // toggle stamps data_sharing_consent_at server-side, preserving an
  // audit trail of opt-in/opt-out moments).
  getDataSharingConsent: async () => {
    const { data, error } = await client.GET('/api/user/data-sharing-consent');
    if (error) _throwApiError(error, 'Failed to get data sharing consent');
    return data as DataSharingConsentResponse;
  },
  updateDataSharingConsent: async (body: DataSharingConsentRequest) => {
    const { data, error } = await client.PUT('/api/user/data-sharing-consent', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update data sharing consent');
    return data as DataSharingConsentResponse;
  },

  // Conversation
  getConversation: async () => {
    const { data, error } = await client.GET('/api/user/conversation');
    if (error) _throwApiError(error, 'Failed to get conversation');
    return data as SessionDetailResponse;
  },
  getConversationSystemPrompt: async () => {
    const { data, error } = await client.GET('/api/user/conversation/system-prompt');
    if (error) _throwApiError(error, 'Failed to get system prompt');
    return data as SessionSystemPromptResponse;
  },

  deleteConversationHistory: async () => {
    const { error } = await client.DELETE('/api/user/conversation/messages');
    if (error) _throwApiError(error, 'Failed to delete conversation history');
  },

  deleteMessage: async (seq: number) => {
    const { error } = await client.DELETE('/api/user/conversation/messages/{seq}' as never, {
      params: { path: { seq } },
    } as never);
    if (error) _throwApiError(error, 'Failed to delete message');
  },

  deleteMessages: async (seqs: number[]) => {
    const { error } = await client.DELETE(
      '/api/user/conversation/messages/batch' as never,
      { body: { seqs } } as never,
    );
    if (error) _throwApiError(error, 'Failed to delete messages');
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

  // Permissions
  getPermissions: async () => {
    const { data, error } = await client.GET('/api/user/permissions');
    if (error) _throwApiError(error, 'Failed to get permissions');
    return data as PermissionsResponse;
  },
  updatePermissions: async (body: PermissionsUpdate) => {
    const { data, error } = await client.PUT('/api/user/permissions', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update permissions');
    return data as PermissionsResponse;
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
  // Calendar config
  getCalendarList: async () => {
    const { data, error } = await client.GET('/api/user/calendar/calendars');
    if (error) _throwApiError(error, 'Failed to list calendars');
    return data as { calendars: Array<{ id: string; summary: string; primary: boolean; access_role: string }> };
  },
  getCalendarConfig: async () => {
    const { data, error } = await client.GET('/api/user/calendar/config');
    if (error) _throwApiError(error, 'Failed to get calendar config');
    return data as { calendars: Array<{ calendar_id: string; display_name: string; disabled_tools: string[]; access_role: string }> };
  },
  updateCalendarConfig: async (body: { calendars: Array<{ calendar_id: string; display_name: string; disabled_tools: string[]; access_role: string }> }) => {
    const { data, error } = await client.PUT('/api/user/calendar/config', {
      body: body as never,
    });
    if (error) _throwApiError(error, 'Failed to update calendar config');
    return data as { calendars: Array<{ calendar_id: string; display_name: string; disabled_tools: string[]; access_role: string }> };
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

  getBlueBubblesLink: async () => {
    const res = await fetch('/api/channels/bluebubbles', { headers: _getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch BlueBubbles link');
    return res.json() as Promise<{ phone_number: string | null; connected: boolean }>;
  },
  setBlueBubblesLink: async (phoneNumber: string) => {
    const res = await fetch('/api/channels/bluebubbles', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._getAuthHeaders() },
      body: JSON.stringify({ phone_number: phoneNumber }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { detail?: string };
      throw new Error(body.detail || `Failed to save: ${res.status}`);
    }
    return res.json() as Promise<{ phone_number: string | null; connected: boolean }>;
  },

  getTwilioLink: async () => {
    const res = await fetch('/api/channels/twilio', { headers: _getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch Twilio link');
    return res.json() as Promise<{ phone_number: string | null; connected: boolean }>;
  },
  setTwilioLink: async (phoneNumber: string) => {
    const res = await fetch('/api/channels/twilio', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._getAuthHeaders() },
      body: JSON.stringify({ phone_number: phoneNumber }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { detail?: string };
      throw new Error(body.detail || `Failed to save: ${res.status}`);
    }
    return res.json() as Promise<{ phone_number: string | null; connected: boolean }>;
  },

  // Activity stream: real-time agent status from any channel.
  // Auto-reconnects on disconnect so transient drops (proxy timeout,
  // network glitch) don't permanently kill the spinner updates.
  // Stops retrying on auth errors (401/403) since reconnecting won't help.
  // Uses exponential backoff for transient errors to avoid server spam.
  subscribeToActivity: (
    onEvent: (event: { type: string; tool_name?: string; channel?: string }) => void,
  ): AbortController => {
    const controller = new AbortController();
    const BASE_MS = 2_000;
    const MAX_MS = 30_000;
    let failures = 0;

    const backoff = (): number => Math.min(BASE_MS * 2 ** failures, MAX_MS);

    const reconnect = (): void => {
      failures++;
      if (!controller.signal.aborted) setTimeout(connect, backoff());
    };

    const connect = (): void => {
      if (controller.signal.aborted) return;
      const token = getAccessToken();

      fetch('/api/user/chat/activity', {
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        signal: controller.signal,
      })
        .then((res) => {
          if (!res.ok || !res.body) {
            if (res.status === 401 || res.status === 403) {
              // Auth error: stop retrying, reconnecting won't fix this
              return;
            }
            reconnect();
            return;
          }
          // Connected successfully, reset backoff
          failures = 0;
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          const read = (): void => {
            reader
              .read()
              .then(({ done, value }) => {
                if (done) {
                  // Server closed the stream: reconnect
                  reconnect();
                  return;
                }
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
                // Stream error: reconnect unless intentionally aborted
                reconnect();
              });
          };
          read();
        })
        .catch(() => {
          // Connection failed: reconnect unless intentionally aborted
          reconnect();
        });
    };

    connect();
    return controller;
  },

  // Chat (async: POST submits, SSE delivers reply -- stays manual)
  sendChatMessage: async (
    message: string,
    files?: File[],
    onEvent?: (event: { type: string; tool_name?: string; content?: string }) => void,
    onAccepted?: (accepted: ChatAccepted) => void,
    uploadOpts?: { onProgress?: (loaded: number, total: number) => void; signal?: AbortSignal },
  ): Promise<ChatResponse> => {
    const formData = new FormData();
    formData.append('message', message);
    if (files) {
      for (const file of files) {
        formData.append('files', file);
      }
    }

    // Step 1: Submit message to bus via XHR (vs. fetch) so we can surface
    // upload-byte progress to the caller and let them abort mid-flight.
    // The 2 minute timeout caps how long a stuck POST can leave the
    // spinner up; once it fires the catch in ChatPage marks the optimistic
    // message as failed so the user can retry. #1368.
    let submitRes: Response;
    try {
      submitRes = await _uploadFormData('/api/user/chat', formData, {
        signal: uploadOpts?.signal,
        onProgress: uploadOpts?.onProgress,
        timeoutMs: 120_000,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === 'TimeoutError') {
        throw new Error('Upload timed out. Please try again.');
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new Error('Upload canceled.');
      }
      throw err;
    }
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

      // EventSource does not support custom headers, so use fetch + ReadableStream.
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
