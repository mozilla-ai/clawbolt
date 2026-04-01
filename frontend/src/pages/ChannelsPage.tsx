import { useState, useEffect, useRef, useMemo } from 'react';
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
import {
  MESSAGING_CHANNELS,
  getChannelState,
  getChannelStatusDisplay,
  type ChannelKey,
  type ChannelState,
} from '@/lib/channel-utils';
import api from '@/api';

// Types for premium linking responses
type TelegramLinkData = Awaited<ReturnType<typeof api.getTelegramLink>>;
type TelegramBotInfo = NonNullable<Awaited<ReturnType<typeof api.getTelegramBotInfo>>>;
type LinqLinkData = Awaited<ReturnType<typeof api.getLinqLink>>;

// Premium data used for state derivation
interface PremiumChannelData {
  telegram_user_id?: string | null;
  phone_number?: string | null;
}

export default function ChannelsPage() {
  const { isPremium } = useAuth();
  const { data: routesData } = useChannelRoutes();
  const { data: channelConfig } = useChannelConfig();
  const toggleMutation = useToggleChannelRoute();

  // Premium link data (fetched once for state derivation)
  const [telegramLinkData, setTelegramLinkData] = useState<TelegramLinkData | null>(null);
  const [linqLinkData, setLinqLinkData] = useState<LinqLinkData | null>(null);
  const [botInfo, setBotInfo] = useState<TelegramBotInfo | null>(null);

  useEffect(() => {
    if (isPremium) {
      api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      api.getTelegramBotInfo().then(setBotInfo).catch(() => {});
      api.getLinqLink().then(setLinqLinkData).catch(() => {});
    }
  }, [isPremium]);

  // Track which config form is expanded
  const [expandedChannel, setExpandedChannel] = useState<ChannelKey | null>(null);
  // Track which channel is switching (optimistic)
  const [switchingChannel, setSwitchingChannel] = useState<ChannelKey | null>(null);

  const routes = routesData?.routes ?? [];

  // Build premium data for state derivation
  const premiumData: PremiumChannelData = {
    telegram_user_id: telegramLinkData?.telegram_user_id,
    phone_number: linqLinkData?.phone_number,
  };

  // Compute states for each channel (memoized to prevent useEffect churn)
  const channelStates = useMemo(() => {
    const states: Record<ChannelKey, ChannelState> = {} as Record<ChannelKey, ChannelState>;
    if (channelConfig) {
      for (const ch of MESSAGING_CHANNELS) {
        states[ch.key] = getChannelState(
          ch.key,
          channelConfig,
          routes,
          isPremium,
          premiumData,
        );
      }
    }
    return states;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelConfig, routesData, isPremium, telegramLinkData, linqLinkData]);

  // Auto-expand the first "available" channel on initial load only
  const hasAutoExpanded = useRef(false);
  useEffect(() => {
    if (!channelConfig || hasAutoExpanded.current) return;
    const needsSetup = MESSAGING_CHANNELS.find(
      (ch) => channelStates[ch.key] === 'available',
    );
    if (needsSetup) {
      setExpandedChannel(needsSetup.key);
      hasAutoExpanded.current = true;
    }
  }, [channelConfig, channelStates]);

  // Find which channel is currently active (if any)
  const activeChannelKey = MESSAGING_CHANNELS.find(
    (ch) => channelStates[ch.key] === 'active',
  )?.key ?? null;

  const handleActivateChannel = (key: ChannelKey) => {
    setSwitchingChannel(key);
    toggleMutation.mutate(
      { channel: key, enabled: true },
      {
        onSuccess: () => {
          setSwitchingChannel(null);
          toast.success(`Switched to ${MESSAGING_CHANNELS.find((c) => c.key === key)?.label}`);
        },
        onError: (e) => {
          setSwitchingChannel(null);
          toast.error(e.message);
        },
      },
    );
  };

  const handleDeactivateAll = () => {
    if (!activeChannelKey) return;
    setSwitchingChannel('none' as ChannelKey);
    toggleMutation.mutate(
      { channel: activeChannelKey, enabled: false },
      {
        onSuccess: () => {
          setSwitchingChannel(null);
          toast.success('Messaging channel deactivated');
        },
        onError: (e) => {
          setSwitchingChannel(null);
          toast.error(e.message);
        },
      },
    );
  };

  const handleToggleExpand = (key: ChannelKey) => {
    setExpandedChannel(expandedChannel === key ? null : key);
  };

  // Callback after config save: collapse form (channel becomes configured)
  const handleConfigSaved = (key: ChannelKey) => {
    // Refresh premium link data after save
    if (isPremium) {
      if (key === 'telegram') api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      if (key === 'linq') api.getLinqLink().then(setLinqLinkData).catch(() => {});
    }
    setExpandedChannel(null);
  };

  // Check if any channels are available at all
  const anyAvailable = channelConfig
    ? MESSAGING_CHANNELS.some((ch) => channelStates[ch.key] !== 'unavailable')
    : true; // Don't show empty state while loading

  // Separate channels into selectable (configured/active) and non-selectable
  const selectableChannels = MESSAGING_CHANNELS.filter(
    (ch) => channelStates[ch.key] === 'configured' || channelStates[ch.key] === 'active',
  );
  const nonSelectableChannels = MESSAGING_CHANNELS.filter(
    (ch) => channelStates[ch.key] === 'unavailable' || channelStates[ch.key] === 'available',
  );

  return (
    <div className="max-w-2xl">
      <h2 className="text-xl font-semibold font-display mb-1">Channels</h2>
      <p className="text-[13px] text-muted-foreground mb-6">
        Choose how your assistant receives messages.
      </p>

      {!anyAvailable && channelConfig ? (
        <Card>
          <div className="text-center py-4">
            <h3 className="text-sm font-medium mb-2">No messaging channels available</h3>
            <p className="text-xs text-muted-foreground mb-4">
              Your server doesn't have any messaging channels configured yet.
              Ask your administrator to set up Telegram, Text Messaging, or BlueBubbles.
            </p>
            <Button onClick={() => window.location.assign('/app/chat')}>Go to Chat</Button>
          </div>
        </Card>
      ) : (
        <div className="grid gap-3">
          {/* Selectable channels (configured + active) in a radio group */}
          {selectableChannels.length > 0 && (
            <div role="radiogroup" aria-label="Active messaging channel">
              <div className="grid gap-3">
                {/* None option */}
                <NoneCard
                  isSelected={!activeChannelKey}
                  isSwitching={switchingChannel === ('none' as ChannelKey)}
                  isMutating={toggleMutation.isPending}
                  onSelect={handleDeactivateAll}
                />
                {selectableChannels.map(({ key, label }) => (
                  <ChannelCard
                    key={key}
                    channelKey={key}
                    label={label}
                    state={channelStates[key]}
                    isExpanded={expandedChannel === key}
                    isSwitching={switchingChannel === key}
                    isMutating={toggleMutation.isPending}
                    onActivate={() => handleActivateChannel(key)}
                    onToggleExpand={() => handleToggleExpand(key)}
                    isPremium={isPremium}
                    channelConfig={channelConfig}
                    botInfo={key === 'telegram' ? botInfo : null}
                    telegramLinkData={key === 'telegram' ? telegramLinkData : null}
                    linqLinkData={key === 'linq' ? linqLinkData : null}
                    onConfigSaved={() => handleConfigSaved(key)}
                    selectable
                  />
                ))}
              </div>
            </div>
          )}

          {/* Non-selectable channels (unavailable + available) */}
          {nonSelectableChannels.map(({ key, label }) => (
            <ChannelCard
              key={key}
              channelKey={key}
              label={label}
              state={channelStates[key]}
              isExpanded={expandedChannel === key}
              isSwitching={false}
              isMutating={toggleMutation.isPending}
              onActivate={() => {}}
              onToggleExpand={() => handleToggleExpand(key)}
              isPremium={isPremium}
              channelConfig={channelConfig}
              botInfo={key === 'telegram' ? botInfo : null}
              telegramLinkData={key === 'telegram' ? telegramLinkData : null}
              linqLinkData={key === 'linq' ? linqLinkData : null}
              onConfigSaved={() => handleConfigSaved(key)}
              selectable={false}
            />
          ))}
        </div>
      )}

      <p className="text-xs text-muted-foreground mt-4">
        Web Chat is always available via the dashboard.
      </p>
    </div>
  );
}

// --- Channel Card ---

interface ChannelCardProps {
  channelKey: ChannelKey;
  label: string;
  state: ChannelState;
  isExpanded: boolean;
  isSwitching: boolean;
  isMutating: boolean;
  onActivate: () => void;
  onToggleExpand: () => void;
  isPremium: boolean;
  channelConfig: ReturnType<typeof useChannelConfig>['data'];
  botInfo: TelegramBotInfo | null;
  telegramLinkData: TelegramLinkData | null;
  linqLinkData: LinqLinkData | null;
  onConfigSaved: () => void;
  selectable: boolean;
}

function ChannelCard({
  channelKey,
  label,
  state,
  isExpanded,
  isSwitching,
  isMutating,
  onActivate,
  onToggleExpand,
  isPremium,
  channelConfig,
  botInfo,
  telegramLinkData,
  linqLinkData,
  onConfigSaved,
  selectable,
}: ChannelCardProps) {
  const status = getChannelStatusDisplay(state);

  const borderClass =
    state === 'active'
      ? 'border-primary'
      : state === 'available'
        ? 'border-warning'
        : 'border-border';
  const bgClass = state === 'active' ? 'bg-primary-light' : '';
  const opacityClass = state === 'unavailable' ? 'opacity-60' : '';

  const subtitleText =
    state === 'unavailable'
      ? getUnavailableHint(channelKey)
      : state === 'available'
        ? 'Server connected. Complete your setup below.'
        : state === 'active'
          ? 'Receiving messages on this channel.'
          : 'Your settings are complete.';

  return (
    <div
      className={`rounded-xl border p-4 transition-colors ${borderClass} ${bgClass} ${opacityClass}`}
      aria-label={`${label}: ${status.label}`}
    >
      {/* Header row */}
      <div className="flex items-center gap-3 min-h-[44px]">
        {/* Radio button for selectable channels */}
        {selectable && (
          <>
            {isSwitching ? (
              <span className="w-4 h-4 shrink-0 flex items-center justify-center">
                <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
              </span>
            ) : (
              <input
                type="radio"
                name="messaging-channel"
                value={channelKey}
                checked={state === 'active'}
                onChange={() => onActivate()}
                disabled={isMutating}
                className="accent-primary w-4 h-4 shrink-0"
              />
            )}
          </>
        )}

        <ChannelIcon channelKey={channelKey} />

        <div className="flex-1 min-w-0">
          <span className="text-sm font-medium">{label}</span>
        </div>

        {/* Status badge */}
        <span
          className={`text-xs px-2 py-0.5 rounded-full font-medium flex items-center gap-1 ${status.badgeBgClass}`}
          aria-hidden="true"
        >
          {state === 'active' && <CheckIcon />}
          {status.label}
        </span>
      </div>

      {/* Subtitle */}
      <p className={`text-xs text-muted-foreground mt-1 ${selectable ? 'ml-7' : 'ml-8'}`}>
        {subtitleText}
      </p>

      {/* Bot info banner for premium Telegram */}
      {channelKey === 'telegram' && botInfo && (state === 'configured' || state === 'active') && (
        <div className="mt-3 ml-7 text-sm">
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
        </div>
      )}

      {/* Config form (for "available" state) */}
      {state === 'available' && (
        <div className="mt-4 ml-8">
          <ChannelConfigForm
            channelKey={channelKey}
            isPremium={isPremium}
            channelConfig={channelConfig}
            telegramLinkData={telegramLinkData}
            linqLinkData={linqLinkData}
            onSaved={onConfigSaved}
          />
        </div>
      )}

      {/* Settings summary (for "configured" and "active") */}
      {(state === 'configured' || state === 'active') && (
        <div className={`mt-3 ${selectable ? 'ml-7' : 'ml-8'}`}>
          <button
            type="button"
            onClick={onToggleExpand}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
            aria-expanded={isExpanded}
          >
            <ChevronIcon expanded={isExpanded} />
            Your settings
          </button>
          {isExpanded && (
            <div className="mt-3">
              <ChannelConfigForm
                channelKey={channelKey}
                isPremium={isPremium}
                channelConfig={channelConfig}
                telegramLinkData={telegramLinkData}
                linqLinkData={linqLinkData}
                onSaved={onConfigSaved}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// --- None Card ---

interface NoneCardProps {
  isSelected: boolean;
  isSwitching: boolean;
  isMutating: boolean;
  onSelect: () => void;
}

function NoneCard({ isSelected, isSwitching, isMutating, onSelect }: NoneCardProps) {
  return (
    <div
      className="rounded-xl border border-border p-4 transition-colors"
      aria-label={`None: ${isSelected ? 'Selected' : 'Not selected'}`}
    >
      <div className="flex items-center gap-3 min-h-[44px]">
        {isSwitching ? (
          <span className="w-4 h-4 shrink-0 flex items-center justify-center">
            <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          </span>
        ) : (
          <input
            type="radio"
            name="messaging-channel"
            value="none"
            checked={isSelected}
            onChange={onSelect}
            disabled={isSelected || isMutating}
            className="accent-primary w-4 h-4 shrink-0"
          />
        )}
        <NoneIcon />
        <div className="flex-1 min-w-0">
          <span className="text-sm font-medium">None</span>
        </div>
      </div>
      <p className="text-xs text-muted-foreground mt-1 ml-7">
        Web chat only. No external messaging channel active.
      </p>
    </div>
  );
}

function NoneIcon() {
  return (
    <svg className="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="9" strokeWidth={1.5} />
      <path strokeLinecap="round" strokeWidth={1.5} d="M6 18L18 6" />
    </svg>
  );
}

function getUnavailableHint(key: ChannelKey): string {
  if (key === 'telegram') return 'Set TELEGRAM_BOT_TOKEN in your environment to enable.';
  if (key === 'linq') return 'Set LINQ_API_TOKEN in your environment to enable.';
  if (key === 'bluebubbles')
    return 'Set BLUEBUBBLES_SERVER_URL and BLUEBUBBLES_PASSWORD in your environment to enable.';
  return '';
}

// --- Config forms ---

interface ChannelConfigFormProps {
  channelKey: ChannelKey;
  isPremium: boolean;
  channelConfig: ReturnType<typeof useChannelConfig>['data'];
  telegramLinkData: TelegramLinkData | null;
  linqLinkData: LinqLinkData | null;
  onSaved: () => void;
}

function ChannelConfigForm({ channelKey, isPremium, ...rest }: ChannelConfigFormProps) {
  if (channelKey === 'telegram') {
    return isPremium ? <PremiumTelegramForm {...rest} /> : <OssTelegramForm {...rest} />;
  }
  if (channelKey === 'linq') {
    return isPremium ? <PremiumLinqForm {...rest} /> : <OssLinqForm {...rest} />;
  }
  if (channelKey === 'bluebubbles') {
    return <BlueBubblesForm {...rest} />;
  }
  return null;
}

// --- Telegram forms ---

function PremiumTelegramForm({
  telegramLinkData,
  onSaved,
}: Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const displayedId = telegramUserId ?? telegramLinkData?.telegram_user_id ?? '';

  const handleSave = async () => {
    if (telegramLinkData && displayedId === (telegramLinkData.telegram_user_id ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await api.setTelegramLink(displayedId);
      setTelegramUserId(null);
      toast.success('Telegram settings updated');
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4">
      <TelegramUserIdField
        value={displayedId}
        onChange={(v) => setTelegramUserId(v)}
      />
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving || telegramLinkData === null} isLoading={saving}>
          Save
        </Button>
      </div>
    </div>
  );
}

function OssTelegramForm({
  channelConfig,
  onSaved,
}: Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? channelConfig?.telegram_allowed_chat_id ?? '';

  const handleSave = () => {
    if (channelConfig && displayedId === channelConfig.telegram_allowed_chat_id) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_allowed_chat_id: displayedId }, {
      onSuccess: () => {
        setTelegramUserId(null);
        toast.success('Telegram settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      <TelegramUserIdField
        value={displayedId}
        onChange={(v) => setTelegramUserId(v)}
      />
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- Linq forms ---

function PremiumLinqForm({
  linqLinkData,
  onSaved,
}: Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const [phoneNumber, setPhoneNumber] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const displayedNumber = phoneNumber ?? linqLinkData?.phone_number ?? '';
  const fromNumber = linqLinkData?.linq_from_number ?? '';

  const handleSave = async () => {
    if (linqLinkData && displayedNumber === (linqLinkData.phone_number ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await api.setLinqLink(displayedNumber);
      setPhoneNumber(null);
      toast.success('Text messaging settings updated');
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
      <Field label="Your Phone Number">
        <Input
          value={displayedNumber}
          onChange={(e) => setPhoneNumber(e.target.value)}
          placeholder="e.g. +15551234567"
          inputMode="tel"
        />
        <p className="text-xs text-muted-foreground mt-1">
          E.164 format phone number. This is the number you'll text from.
        </p>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving || linqLinkData === null} isLoading={saving}>
          Save
        </Button>
      </div>
    </div>
  );
}

const LINQ_SERVICES = ['iMessage', 'SMS', 'RCS'] as const;

function OssLinqForm({
  channelConfig,
  onSaved,
}: Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumber, setAllowedNumber] = useState<string | null>(null);
  const [preferredService, setPreferredService] = useState<string | null>(null);

  const displayedNumber = allowedNumber ?? channelConfig?.linq_allowed_numbers ?? '';
  const displayedService = preferredService ?? channelConfig?.linq_preferred_service ?? 'iMessage';
  const fromNumber = channelConfig?.linq_from_number ?? '';

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumber !== null && allowedNumber !== (channelConfig?.linq_allowed_numbers ?? '')) {
      updates.linq_allowed_numbers = allowedNumber;
    }
    if (preferredService !== null && preferredService !== (channelConfig?.linq_preferred_service ?? 'iMessage')) {
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
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
      <Field label="Allowed Phone Number">
        <Input
          value={displayedNumber}
          onChange={(e) => setAllowedNumber(e.target.value)}
          placeholder="e.g. +15551234567"
          inputMode="tel"
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
        >
          {LINQ_SERVICES.map((svc) => (
            <option key={svc} value={svc}>{svc}</option>
          ))}
        </Select>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- BlueBubbles form ---

function BlueBubblesForm({
  channelConfig,
  onSaved,
}: Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumbers, setAllowedNumbers] = useState<string | null>(null);
  const [imessageAddress, setImessageAddress] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? channelConfig?.bluebubbles_allowed_numbers ?? '';
  const displayedAddress = imessageAddress ?? channelConfig?.bluebubbles_imessage_address ?? '';

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumbers !== null && allowedNumbers !== (channelConfig?.bluebubbles_allowed_numbers ?? '')) {
      updates.bluebubbles_allowed_numbers = allowedNumbers;
    }
    if (imessageAddress !== null && imessageAddress !== (channelConfig?.bluebubbles_imessage_address ?? '')) {
      updates.bluebubbles_imessage_address = imessageAddress;
    }
    if (Object.keys(updates).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(updates, {
      onSuccess: () => {
        setAllowedNumbers(null);
        setImessageAddress(null);
        toast.success('BlueBubbles settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      {displayedAddress && (
        <TextAssistantCard
          fromNumber={displayedAddress}
          subtitle="Send an iMessage to this address to reach your assistant."
        />
      )}
      <Field label="iMessage Address">
        <Input
          value={displayedAddress}
          onChange={(e) => setImessageAddress(e.target.value)}
          placeholder="e.g. user@icloud.com or +15551234567"
        />
        <p className="text-xs text-muted-foreground mt-1">
          The iCloud email or phone number people should text to reach your assistant.
        </p>
      </Field>
      <Field label="Allowed Sender">
        <Input
          value={displayedNumbers}
          onChange={(e) => setAllowedNumbers(e.target.value)}
          placeholder="e.g. +15551234567 or user@icloud.com"
        />
        <p className="text-xs text-muted-foreground mt-1">
          E.164 phone number or iCloud email, or * to allow all. Empty = deny all.
        </p>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- Shared UI components ---

function CheckIcon() {
  return (
    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
    </svg>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`w-3 h-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
    </svg>
  );
}

function ChannelIcon({ channelKey }: { channelKey: ChannelKey }) {
  if (channelKey === 'telegram') {
    return (
      <svg className="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
      </svg>
    );
  }
  if (channelKey === 'linq') {
    return (
      <svg className="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
      </svg>
    );
  }
  // bluebubbles
  return (
    <svg className="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a2 2 0 01-2-2v-1M13 4H7a2 2 0 00-2 2v6a2 2 0 002 2h2v4l4-4h2a2 2 0 002-2V6a2 2 0 00-2-2z" />
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
