import type { ChannelConfigResponse, ChannelRouteResponse } from '@/types';

export type ChannelState = 'unavailable' | 'available' | 'configured' | 'active';

export type ChannelKey = (typeof MESSAGING_CHANNELS)[number]['key'];

export const MESSAGING_CHANNELS = [
  { key: 'telegram', label: 'Telegram' },
  { key: 'linq', label: 'Text Messaging (iMessage / RCS / SMS)' },
  { key: 'bluebubbles', label: 'BlueBubbles (iMessage)' },
] as const;

/** Whether the server has the necessary credentials/config for this channel. */
function isServerAvailable(key: ChannelKey, config: ChannelConfigResponse): boolean {
  if (key === 'telegram') return config.telegram_bot_token_set;
  if (key === 'linq') return config.linq_api_token_set;
  if (key === 'bluebubbles') return config.bluebubbles_configured;
  return false;
}

/** Whether the user has completed their side of the configuration. */
function isUserConfigured(
  key: ChannelKey,
  config: ChannelConfigResponse,
  isPremium: boolean,
  premiumData?: { telegram_user_id?: string | null; phone_number?: string | null },
): boolean {
  if (key === 'telegram') {
    if (isPremium) return !!(premiumData?.telegram_user_id);
    return config.telegram_allowed_chat_id !== '';
  }
  if (key === 'linq') {
    if (isPremium) return !!(premiumData?.phone_number);
    return config.linq_allowed_numbers !== '';
  }
  if (key === 'bluebubbles') {
    return config.bluebubbles_allowed_numbers !== '';
  }
  return false;
}

/** Derive the full channel state from server config, user config, and routes. */
export function getChannelState(
  key: ChannelKey,
  config: ChannelConfigResponse,
  routes: ChannelRouteResponse[],
  isPremium: boolean,
  premiumData?: { telegram_user_id?: string | null; phone_number?: string | null },
): ChannelState {
  if (!isServerAvailable(key, config)) return 'unavailable';

  const hasActiveRoute = routes.some((r) => r.channel === key && r.enabled);
  if (hasActiveRoute && isUserConfigured(key, config, isPremium, premiumData)) return 'active';

  if (isUserConfigured(key, config, isPremium, premiumData)) return 'configured';

  return 'available';
}

interface StatusDisplay {
  label: string;
  dotClass: string;
  labelClass: string;
  badgeBgClass: string;
}

export function getChannelStatusDisplay(state: ChannelState): StatusDisplay {
  switch (state) {
    case 'unavailable':
      return {
        label: 'Not available',
        dotClass: 'bg-muted-foreground',
        labelClass: 'text-muted-foreground',
        badgeBgClass: 'bg-muted text-muted-foreground',
      };
    case 'available':
      return {
        label: 'Setup needed',
        dotClass: 'bg-warning',
        labelClass: 'text-warning',
        badgeBgClass: 'bg-warning-bg text-warning',
      };
    case 'configured':
      return {
        label: 'Ready',
        dotClass: 'bg-info',
        labelClass: 'text-info',
        badgeBgClass: 'bg-info-bg text-info',
      };
    case 'active':
      return {
        label: 'Active',
        dotClass: 'bg-success',
        labelClass: 'text-success',
        badgeBgClass: 'bg-success-bg text-success',
      };
  }
}
