import type { ChannelConfigResponse, ChannelRouteResponse } from '@/types';
import type { PremiumLinkData } from '@/components/ChannelConfigForm';

export type ChannelState = 'unavailable' | 'available' | 'configured' | 'active';

export type ChannelKey = (typeof MESSAGING_CHANNELS)[number]['key'];

// Real backend channel keys. `linq` and `bluebubbles` are both rendered to the
// user as "iMessage"; which one shows up at runtime is determined by which
// backend the admin has configured. Users never see the backend name.
export const MESSAGING_CHANNELS = [
  { key: 'telegram', label: 'Telegram' },
  { key: 'linq', label: 'iMessage' },
  { key: 'bluebubbles', label: 'iMessage' },
] as const;

/** Return the subset of MESSAGING_CHANNELS the user should see, filtered by
 * which iMessage backend (if any) the admin has configured. The mutual
 * exclusion between Linq and BlueBubbles is enforced at server startup, so at
 * most one of them will ever be present in the visible list. */
export function getVisibleChannels(
  config: ChannelConfigResponse | undefined,
): ReadonlyArray<(typeof MESSAGING_CHANNELS)[number]> {
  // Before the config loads we can't know which iMessage backend (if any) to
  // render, so we show only the always-available entries. Once config arrives
  // we include the single iMessage card matching config.imessage_backend.
  if (!config) return MESSAGING_CHANNELS.filter((ch) => ch.key === 'telegram');
  return MESSAGING_CHANNELS.filter((ch) => {
    if (ch.key === 'telegram') return true;
    return ch.key === config.imessage_backend;
  });
}

/** Premium link data keyed by channel. Adding a channel here is all that's needed. */
export type PremiumChannelData = {
  telegram_user_id?: string | null;
  linkData: Partial<Record<ChannelKey, PremiumLinkData | null>>;
};

/** Whether the server has the necessary credentials/config for this channel. */
export function isServerAvailable(key: ChannelKey, config: ChannelConfigResponse): boolean {
  if (key === 'telegram') return config.telegram_bot_token_set;
  if (key === 'linq') return config.imessage_backend === 'linq';
  if (key === 'bluebubbles') return config.imessage_backend === 'bluebubbles';
  return false;
}

/** Whether the user has completed their side of the configuration. */
function isUserConfigured(
  key: ChannelKey,
  config: ChannelConfigResponse,
  isPremium: boolean,
  premiumData?: PremiumChannelData,
): boolean {
  if (key === 'telegram') {
    if (isPremium) return !!(premiumData?.telegram_user_id);
    return config.telegram_allowed_chat_id !== '';
  }
  // Generic premium link check: if premium and linkData exists, use it
  if (isPremium) {
    const link = premiumData?.linkData[key];
    if (link !== undefined) return !!(link?.identifier);
  }
  if (key === 'linq') {
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
  premiumData?: PremiumChannelData,
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
