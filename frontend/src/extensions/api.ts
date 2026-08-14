import client, { tryRefresh } from '@/lib/api-client';
import type { AuthUser } from '@/types';
import type { UsageSummary } from './types';

function _throwApiError(error: unknown, fallback: string): never {
  const b = error as { detail?: string };
  throw new Error(b.detail || fallback);
}

export async function tryRestoreSession(): Promise<AuthUser | null> {
  const refreshed = await tryRefresh();
  if (!refreshed) return null;

  try {
    const { data, error } = await client.GET('/api/account/profile');
    if (error) return null;
    const profile = data as { id: string; email?: string; name?: string; role?: string };
    return {
      id: profile.id,
      name: profile.name ?? profile.email ?? profile.id,
      role: profile.role,
    } as unknown as AuthUser;
  } catch {
    return null;
  }
}

export async function getSubscription(): Promise<UsageSummary> {
  const { data, error } = await client.GET('/api/account/usage');
  if (error) _throwApiError(error, 'Failed to get usage');
  return data as UsageSummary;
}

export interface TelegramBotInfo {
  bot_username: string | null;
  bot_link: string | null;
}

export async function getTelegramBotInfo(): Promise<TelegramBotInfo> {
  const { data, error } = await client.GET('/api/channels/telegram/bot-info');
  if (error) _throwApiError(error, 'Failed to get bot info');
  return data as TelegramBotInfo;
}
