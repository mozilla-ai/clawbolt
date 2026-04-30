import { useMemo } from 'react';
import {
  useChannelRoutes,
  useChannelConfig,
  useTelegramLink,
  useTelegramBotInfo,
  useLinqLink,
  useBlueBubblesLink,
} from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import {
  MESSAGING_CHANNELS,
  getChannelState,
  type ChannelKey,
  type ChannelState,
  type PremiumChannelData,
} from '@/lib/channel-utils';
import type { ChannelConfigResponse } from '@/types';
import type { PremiumLinkData, TelegramLinkData } from '@/components/ChannelConfigForm';

type TelegramBotInfo = NonNullable<Awaited<ReturnType<typeof useTelegramBotInfo>>['data']>;

export interface ChannelStatesResult {
  /** Per-channel derived state (keys absent until config loads). */
  states: Partial<Record<ChannelKey, ChannelState>>;
  /** True while any core query is still loading for the first time. */
  isLoading: boolean;
  /** True if any core query errored and has no cached data. */
  isError: boolean;
  /** Server-level channel configuration. */
  channelConfig: ChannelConfigResponse | undefined;
  /** Premium link data map for linq/bluebubbles (for ChannelConfigForm). */
  linkDataMap: Partial<Record<ChannelKey, PremiumLinkData | null>>;
  /** Telegram-specific premium link data (for ChannelConfigForm). */
  telegramLinkData: TelegramLinkData | null;
  /** Telegram bot info (premium only). */
  botInfo: TelegramBotInfo | null;
  /** Refresh premium link data for a specific channel after config save. */
  invalidateLink: (key: ChannelKey) => void;
}

function normalizeLinkData(data: { phone_number: string | null; connected: boolean }): PremiumLinkData {
  return { identifier: data.phone_number, connected: data.connected };
}

/**
 * Single source of truth for channel state derivation.
 * Fetches all required data (core + premium links) and derives per-channel states.
 */
export function useChannelStates(): ChannelStatesResult {
  const { isPremium } = useAuth();

  // Core queries (shared via React Query cache)
  const routesQuery = useChannelRoutes();
  const configQuery = useChannelConfig();

  const routes = routesQuery.data?.routes ?? [];
  const channelConfig = configQuery.data;

  // Premium link queries (only fire when isPremium).
  // Bot info is additionally gated on telegram_bot_token_set: in premium
  // prod, tenants are routed via tenant-specific bots and the global
  // TELEGRAM_BOT_TOKEN env var is empty, so the OSS /bot-info endpoint
  // returns 404 (issue #332). Skipping the call when no bot token is
  // configured eliminates the noise without touching backend behavior.
  const telegramBotTokenSet = channelConfig?.telegram_bot_token_set ?? false;
  const telegramLinkQuery = useTelegramLink(isPremium);
  const telegramBotInfoQuery = useTelegramBotInfo(isPremium && telegramBotTokenSet);
  const linqLinkQuery = useLinqLink(isPremium);
  const blueBubblesLinkQuery = useBlueBubblesLink(isPremium);

  // Build premium data from React Query results
  const linkDataMap = useMemo<Partial<Record<ChannelKey, PremiumLinkData | null>>>(() => {
    const map: Partial<Record<ChannelKey, PremiumLinkData | null>> = {};
    if (linqLinkQuery.data) map.linq = normalizeLinkData(linqLinkQuery.data);
    if (blueBubblesLinkQuery.data) map.bluebubbles = normalizeLinkData(blueBubblesLinkQuery.data);
    return map;
  }, [linqLinkQuery.data, blueBubblesLinkQuery.data]);

  const telegramLinkData = telegramLinkQuery.data ?? null;
  const telegramUserId = telegramLinkData?.telegram_user_id;
  const botInfo = telegramBotInfoQuery.data ?? null;

  // Derive states (deps are all primitives or memoized objects for stable identity)
  const states = useMemo<Partial<Record<ChannelKey, ChannelState>>>(() => {
    if (!channelConfig) return {};
    const premiumData: PremiumChannelData | undefined = isPremium
      ? { telegram_user_id: telegramUserId, linkData: linkDataMap }
      : undefined;
    const result: Partial<Record<ChannelKey, ChannelState>> = {};
    for (const ch of MESSAGING_CHANNELS) {
      result[ch.key] = getChannelState(ch.key, channelConfig, routes, isPremium, premiumData);
    }
    return result;
  }, [channelConfig, routes, isPremium, telegramUserId, linkDataMap]);

  // Invalidation helper for after config saves
  const invalidateLink = (key: ChannelKey) => {
    if (!isPremium) return;
    if (key === 'telegram') void telegramLinkQuery.refetch();
    if (key === 'linq') void linqLinkQuery.refetch();
    if (key === 'bluebubbles') void blueBubblesLinkQuery.refetch();
  };

  return {
    states,
    isLoading:
      (routesQuery.isPending && !routesQuery.data) ||
      (configQuery.isPending && !configQuery.data),
    isError:
      (routesQuery.isError && !routesQuery.data) ||
      (configQuery.isError && !configQuery.data),
    channelConfig,
    linkDataMap,
    telegramLinkData,
    botInfo,
    invalidateLink,
  };
}
