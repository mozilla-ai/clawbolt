import type { AuthConfig } from '@/types';

export function isPremiumAuth(config: AuthConfig | null): boolean {
  return config?.method === 'oauth_google';
}
