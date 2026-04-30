import { describe, it, expect } from 'vitest';
import { getVisibleChannels } from '../channel-utils';
import type { ChannelConfigResponse } from '@/types';

const baseConfig: ChannelConfigResponse = {
  telegram_bot_token_set: false,
  telegram_allowed_chat_id: '',
  linq_api_token_set: false,
  linq_from_number: '',
  linq_allowed_numbers: '',
  linq_preferred_service: 'iMessage',
  bluebubbles_configured: false,
  bluebubbles_allowed_numbers: '',
  bluebubbles_imessage_address: '',
  imessage_backend: null,
};

describe('getVisibleChannels', () => {
  it('returns an empty list while config is undefined', () => {
    expect(getVisibleChannels(undefined)).toEqual([]);
  });

  it('hides every channel when nothing is configured server-side', () => {
    expect(getVisibleChannels(baseConfig)).toEqual([]);
  });

  it('shows only Telegram when only the bot token is set', () => {
    const config: ChannelConfigResponse = { ...baseConfig, telegram_bot_token_set: true };
    const result = getVisibleChannels(config);
    expect(result.map((c) => c.key)).toEqual(['telegram']);
  });

  it('shows only Linq when imessage_backend=linq and Telegram is not configured', () => {
    const config: ChannelConfigResponse = { ...baseConfig, imessage_backend: 'linq' };
    const result = getVisibleChannels(config);
    expect(result.map((c) => c.key)).toEqual(['linq']);
  });

  it('shows Telegram and BlueBubbles when both are configured', () => {
    const config: ChannelConfigResponse = {
      ...baseConfig,
      telegram_bot_token_set: true,
      imessage_backend: 'bluebubbles',
    };
    const result = getVisibleChannels(config);
    expect(result.map((c) => c.key)).toEqual(['telegram', 'bluebubbles']);
  });

  it('does not show the unselected iMessage backend', () => {
    const config: ChannelConfigResponse = { ...baseConfig, imessage_backend: 'linq' };
    const result = getVisibleChannels(config);
    expect(result.find((c) => c.key === 'bluebubbles')).toBeUndefined();
  });
});
