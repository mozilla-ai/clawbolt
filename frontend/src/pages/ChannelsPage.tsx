import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import TextAssistantCard from '@/components/TextAssistantCard';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Field from '@/components/ui/field';
import Select from '@/components/ui/select';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useChannelConfig, useUpdateChannelConfig, useChannelRoutes, useToggleChannelRoute } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import api from '@/api';
import type { ChannelRouteResponse } from '@/types';
import type { AppShellContext } from '@/layouts/AppShell';

// Messaging channel definitions (webchat excluded, it is always available)
const MESSAGING_CHANNELS = [
  { key: 'telegram', label: 'Telegram' },
  { key: 'linq', label: 'Text Messaging (iMessage / RCS / SMS)' },
  { key: 'bluebubbles', label: 'BlueBubbles (iMessage)' },
] as const;

type ChannelKey = (typeof MESSAGING_CHANNELS)[number]['key'];

export default function ChannelsPage() {
  const { isPremium } = useAuth();
  const { profile } = useOutletContext<AppShellContext>();
  const { data: routesData } = useChannelRoutes();
  const { data: channelConfig } = useChannelConfig();
  const toggleMutation = useToggleChannelRoute();

  // Determine which channel is currently active:
  // 1. Find the enabled route (should be at most one after enforcement)
  // 2. Fall back to preferred_channel from profile
  // 3. Fall back to null (no selection)
  const enabledRoute = routesData?.routes.find(
    (r: ChannelRouteResponse) => r.enabled && r.channel !== 'webchat',
  );
  const preferredChannel = profile?.preferred_channel;
  const defaultChannel =
    enabledRoute?.channel ??
    (preferredChannel && preferredChannel !== 'webchat' ? preferredChannel : null);

  const [selectedChannel, setSelectedChannel] = useState<ChannelKey | null>(null);
  // Track which channel was just confirmed so we can show the Active badge
  const [confirmedChannel, setConfirmedChannel] = useState<ChannelKey | null>(null);

  // Sync selected channel with backend state
  useEffect(() => {
    if (defaultChannel && MESSAGING_CHANNELS.some((c) => c.key === defaultChannel)) {
      setSelectedChannel(defaultChannel as ChannelKey);
      setConfirmedChannel(defaultChannel as ChannelKey);
    }
  }, [defaultChannel]);

  const handleSelectChannel = (channel: ChannelKey) => {
    setSelectedChannel(channel);
    toggleMutation.mutate(
      { channel, enabled: true },
      {
        onSuccess: () => {
          setConfirmedChannel(channel);
          toast.success(`Switched to ${MESSAGING_CHANNELS.find((c) => c.key === channel)?.label}`);
        },
        onError: (e) => {
          // Revert optimistic selection on failure
          setSelectedChannel(confirmedChannel);
          toast.error(e.message);
        },
      },
    );
  };

  const isChannelConfigured = (channel: string): boolean => {
    if (channel === 'telegram') return channelConfig?.telegram_bot_token_set ?? false;
    if (channel === 'linq') return channelConfig?.linq_api_token_set ?? false;
    if (channel === 'bluebubbles') return channelConfig?.bluebubbles_configured ?? false;
    return false;
  };

  return (
    <div className="max-w-2xl">
      <h2 className="text-xl font-semibold font-display mb-6">Channels</h2>

      {/* Channel selector */}
      <Card>
        <h3 className="text-sm font-medium mb-4">Select your messaging channel</h3>
        <div className="grid gap-2" role="radiogroup" aria-label="Messaging channel">
          {MESSAGING_CHANNELS.map(({ key, label }) => {
            const configured = isChannelConfigured(key);
            const isSelected = selectedChannel === key;
            const isConfirmed = confirmedChannel === key;
            const isDisabled = !configured && !isConfirmed;
            const isSwitching = toggleMutation.isPending && isSelected && !isConfirmed;
            return (
              <label
                key={key}
                className={`flex items-center gap-3 p-3 rounded-xl border transition-colors ${
                  isDisabled
                    ? 'opacity-50 cursor-not-allowed'
                    : isSelected
                      ? 'border-primary bg-primary-light cursor-pointer'
                      : 'border-border hover:border-primary/40 cursor-pointer'
                }`}
              >
                {isSwitching ? (
                  <span className="w-4 h-4 shrink-0 flex items-center justify-center">
                    <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
                  </span>
                ) : (
                  <input
                    type="radio"
                    name="messaging-channel"
                    value={key}
                    checked={isSelected}
                    onChange={() => handleSelectChannel(key)}
                    disabled={isDisabled || toggleMutation.isPending}
                    className="accent-primary w-4 h-4 shrink-0"
                  />
                )}
                <span className="flex-1 text-sm font-medium">{label}</span>
                {isConfirmed ? (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-primary-light text-primary font-medium flex items-center gap-1">
                    <CheckIcon />
                    Active
                  </span>
                ) : configured ? (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
                    Connected
                  </span>
                ) : (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
                    Not configured
                  </span>
                )}
              </label>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground mt-4">
          Web Chat is always available via the dashboard.
        </p>
      </Card>

      {/* Channel configuration (shown for selected channel) */}
      {selectedChannel && (
        <div className="mt-6">
          {selectedChannel === 'telegram' && (
            isPremium ? <PremiumTelegramSection /> : <OssTelegramSection />
          )}
          {selectedChannel === 'linq' && (
            isPremium ? <PremiumTextMessagingSection /> : <TextMessagingSection />
          )}
          {selectedChannel === 'bluebubbles' && (
            <BlueBubblesSection />
          )}
        </div>
      )}
    </div>
  );
}

// Types for premium linking responses (inferred from api module)
type TelegramLinkData = Awaited<ReturnType<typeof api.getTelegramLink>>;
type TelegramBotInfo = NonNullable<Awaited<ReturnType<typeof api.getTelegramBotInfo>>>;
type LinqLinkData = Awaited<ReturnType<typeof api.getLinqLink>>;

// --- Telegram section ---

function PremiumTelegramSection() {
  const { data: channelConfig } = useChannelConfig();
  const [linkData, setLinkData] = useState<TelegramLinkData | null>(null);
  const [botInfo, setBotInfo] = useState<TelegramBotInfo | null>(null);
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const isConfigured = channelConfig?.telegram_bot_token_set ?? false;

  useEffect(() => {
    api.getTelegramLink().then(setLinkData).catch(() => {});
    api.getTelegramBotInfo().then(setBotInfo).catch(() => {});
  }, []);

  const displayedId = telegramUserId ?? linkData?.telegram_user_id ?? '';

  const handleSave = async () => {
    if (linkData && displayedId === (linkData.telegram_user_id ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      const result = await api.setTelegramLink(displayedId);
      setLinkData(result);
      setTelegramUserId(null);
      toast.success('Telegram settings updated');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-6">
      {botInfo && (
        <Card>
          <div className="flex items-center gap-3">
            <span className="text-sm">
              Message{' '}
              <a
                href={botInfo.bot_link}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-primary hover:underline"
              >
                @{botInfo.bot_username}
              </a>
              {' '}on Telegram to chat with your assistant.
            </span>
          </div>
        </Card>
      )}

      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Telegram Configuration</h3>
          {!isConfigured && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
              Not configured
            </span>
          )}
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            A Telegram bot token must be configured by an administrator to enable this channel.
          </p>
        )}
        <div className={`grid gap-4${!isConfigured ? ' opacity-50 pointer-events-none' : ''}`}>
          <TelegramUserIdField
            value={displayedId}
            onChange={(v) => setTelegramUserId(v)}
            disabled={!isConfigured}
          />
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || saving || linkData === null} isLoading={saving}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function OssTelegramSection() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? config?.telegram_allowed_chat_id ?? '';
  const isConfigured = config?.telegram_bot_token_set ?? false;

  const handleSave = () => {
    if (config && displayedId === config.telegram_allowed_chat_id) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_allowed_chat_id: displayedId }, {
      onSuccess: () => {
        setTelegramUserId(null);
        toast.success('Telegram settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-6">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Telegram Configuration</h3>
          <span className={`text-xs px-2 py-0.5 rounded-full ${isConfigured ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
            {isConfigured ? 'Connected' : 'Not configured'}
          </span>
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            Set <code className="font-mono text-[11px]">TELEGRAM_BOT_TOKEN</code> in your environment
            or in <a href="/app/settings/telegram" className="underline">Settings &gt; Telegram</a> to enable.
          </p>
        )}
        <div className={`grid gap-4${!isConfigured ? ' opacity-50 pointer-events-none' : ''}`}>
          <TelegramUserIdField
            value={displayedId}
            onChange={(v) => setTelegramUserId(v)}
            disabled={!isConfigured}
          />
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function CheckIcon() {
  return (
    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
    </svg>
  );
}

function InfoIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="10" strokeWidth="2" />
      <path strokeWidth="2" strokeLinecap="round" d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

const TELEGRAM_ID_TOOLTIP =
  'Clawbolt uses your numeric ID because Telegram usernames are optional' +
  ' and can change at any time. The numeric ID is permanent and will' +
  ' always identify your account.';

function TelegramUserIdField({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <Field label="Your Telegram User ID">
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. 123456789"
        inputMode="numeric"
        disabled={disabled}
      />
      <p className="text-xs text-muted-foreground mt-1">
        Your numeric Telegram user ID. Send /start to @userinfobot on Telegram to find it.{' '}
        <Tooltip content={TELEGRAM_ID_TOOLTIP} delay={400} closeDelay={0}>
          <span className="inline-flex items-center align-middle cursor-help text-muted-foreground/70 hover:text-muted-foreground">
            <InfoIcon />
            <span className="ml-0.5 underline decoration-dotted">Why not my username?</span>
          </span>
        </Tooltip>
      </p>
    </Field>
  );
}

// --- Premium Linq section ---

function PremiumTextMessagingSection() {
  const { data: channelConfig } = useChannelConfig();
  const [linkData, setLinkData] = useState<LinqLinkData | null>(null);
  const [phoneNumber, setPhoneNumber] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const isConfigured = channelConfig?.linq_api_token_set ?? false;

  useEffect(() => {
    api.getLinqLink().then(setLinkData).catch(() => {});
  }, []);

  const displayedNumber = phoneNumber ?? linkData?.phone_number ?? '';
  const fromNumber = linkData?.linq_from_number ?? '';

  const handleSave = async () => {
    if (linkData && displayedNumber === (linkData.phone_number ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      const result = await api.setLinqLink(displayedNumber);
      setLinkData(result);
      setPhoneNumber(null);
      toast.success('Text messaging settings updated');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-6">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Text Messaging Configuration</h3>
          {!isConfigured && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
              Not configured
            </span>
          )}
        </div>
        <div className={`grid gap-4${!isConfigured ? ' opacity-50 pointer-events-none' : ''}`}>
          <Field label="Your Phone Number">
            <Input
              value={displayedNumber}
              onChange={(e) => setPhoneNumber(e.target.value)}
              placeholder="e.g. +15551234567"
              inputMode="tel"
              disabled={!isConfigured}
            />
            <p className="text-xs text-muted-foreground mt-1">
              E.164 format phone number. This is the number you'll text from.
            </p>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || saving || linkData === null} isLoading={saving}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

// --- OSS Linq section ---

const LINQ_SERVICES = ['iMessage', 'SMS', 'RCS'] as const;

function TextMessagingSection() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumber, setAllowedNumber] = useState<string | null>(null);
  const [preferredService, setPreferredService] = useState<string | null>(null);

  const displayedNumber = allowedNumber ?? config?.linq_allowed_numbers ?? '';
  const displayedService = preferredService ?? config?.linq_preferred_service ?? 'iMessage';
  const isConfigured = config?.linq_api_token_set ?? false;

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumber !== null && allowedNumber !== (config?.linq_allowed_numbers ?? '')) {
      updates.linq_allowed_numbers = allowedNumber;
    }
    if (preferredService !== null && preferredService !== (config?.linq_preferred_service ?? 'iMessage')) {
      updates.linq_preferred_service = preferredService;
    }
    if (Object.keys(updates).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(updates, {
      onSuccess: () => {
        setAllowedNumber(null);
        setPreferredService(null);
        toast.success('Linq settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  const fromNumber = config?.linq_from_number ?? '';

  return (
    <div className="grid gap-6">
      {isConfigured && fromNumber && (
        <TextAssistantCard fromNumber={fromNumber} />
      )}
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Text Messaging Configuration</h3>
          <span className={`text-xs px-2 py-0.5 rounded-full ${isConfigured ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
            {isConfigured ? 'Connected' : 'Not configured'}
          </span>
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            Let users text your assistant from their phone's native messaging app.
            Set <code className="font-mono text-[11px]">LINQ_API_TOKEN</code> in your environment to enable.
          </p>
        )}
        <div className={`grid gap-4${!isConfigured ? ' opacity-50 pointer-events-none' : ''}`}>
          <Field label="Allowed Phone Number">
            <Input
              value={displayedNumber}
              onChange={(e) => setAllowedNumber(e.target.value)}
              placeholder="e.g. +15551234567"
              inputMode="tel"
              disabled={!isConfigured}
            />
            <p className="text-xs text-muted-foreground mt-1">
              E.164 phone number, or * to allow all. Empty = deny all.
            </p>
          </Field>
          <Field label="Preferred Service">
            <Select
              value={displayedService}
              onChange={(e) => setPreferredService(e.target.value)}
              aria-label="Preferred messaging service"
              disabled={!isConfigured}
            >
              {LINQ_SERVICES.map((svc) => (
                <option key={svc} value={svc}>{svc}</option>
              ))}
            </Select>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

// --- BlueBubbles section ---

function BlueBubblesSection() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumbers, setAllowedNumbers] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? config?.bluebubbles_allowed_numbers ?? '';
  const isConfigured = config?.bluebubbles_configured ?? false;

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumbers !== null && allowedNumbers !== (config?.bluebubbles_allowed_numbers ?? '')) {
      updates.bluebubbles_allowed_numbers = allowedNumbers;
    }
    if (Object.keys(updates).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(updates, {
      onSuccess: () => {
        setAllowedNumbers(null);
        toast.success('BlueBubbles settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium">BlueBubbles Configuration</h3>
        <span className={`text-xs px-2 py-0.5 rounded-full ${isConfigured ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
          {isConfigured ? 'Connected' : 'Not configured'}
        </span>
      </div>
      {!isConfigured && (
        <p className="text-xs text-muted-foreground mb-4">
          Self-hosted iMessage bridge via a Mac with BlueBubbles.
          Set <code className="font-mono text-[11px]">BLUEBUBBLES_SERVER_URL</code> and{' '}
          <code className="font-mono text-[11px]">BLUEBUBBLES_PASSWORD</code> in your environment to enable.
        </p>
      )}
      <div className={`grid gap-4${!isConfigured ? ' opacity-50 pointer-events-none' : ''}`}>
        <Field label="Allowed Sender">
          <Input
            value={displayedNumbers}
            onChange={(e) => setAllowedNumbers(e.target.value)}
            placeholder="e.g. +15551234567 or user@icloud.com"
            disabled={!isConfigured}
          />
          <p className="text-xs text-muted-foreground mt-1">
            E.164 phone number or iCloud email, or * to allow all. Empty = deny all.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={!isConfigured || updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
            Save
          </Button>
        </div>
      </div>
    </Card>
  );
}
