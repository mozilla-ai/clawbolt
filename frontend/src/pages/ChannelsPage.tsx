import { useState, useEffect, useRef } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { toast } from '@/lib/toast';
import { useToggleChannelRoute } from '@/hooks/queries';
import { useChannelStates } from '@/hooks/useChannelStates';
import { useAuth } from '@/contexts/AuthContext';
import {
  getVisibleChannels,
  getChannelStatusDisplay,
  type ChannelKey,
  type ChannelState,
} from '@/lib/channel-utils';
import { ChannelConfigForm, type TelegramLinkData, type PremiumLinkData } from '@/components/ChannelConfigForm';
import type { ChannelStatesResult } from '@/hooks/useChannelStates';

type TelegramBotInfo = ChannelStatesResult['botInfo'];

export default function ChannelsPage() {
  const { isPremium } = useAuth();
  const toggleMutation = useToggleChannelRoute();
  const { states: channelStates, channelConfig, telegramLinkData, botInfo, linkDataMap, invalidateLink } = useChannelStates();
  const visibleChannels = getVisibleChannels(channelConfig);

  // Track which config form is expanded
  const [expandedChannel, setExpandedChannel] = useState<ChannelKey | null>(null);
  // Track which channel is switching (optimistic)
  const [switchingChannel, setSwitchingChannel] = useState<ChannelKey | null>(null);

  // Auto-expand the first "available" channel on initial load only
  const hasAutoExpanded = useRef(false);
  useEffect(() => {
    if (!channelConfig || hasAutoExpanded.current) return;
    const needsSetup = visibleChannels.find(
      (ch) => channelStates[ch.key] === 'available',
    );
    if (needsSetup) {
      setExpandedChannel(needsSetup.key);
      hasAutoExpanded.current = true;
    }
  }, [channelConfig, channelStates]);

  // Find which channel is currently active (if any)
  const activeChannelKey = visibleChannels.find(
    (ch) => channelStates[ch.key] === 'active',
  )?.key ?? null;

  const handleActivateChannel = (key: ChannelKey) => {
    setSwitchingChannel(key);
    toggleMutation.mutate(
      { channel: key, enabled: true },
      {
        onSuccess: () => {
          setSwitchingChannel(null);
          toast.success(`Switched to ${visibleChannels.find((c) => c.key === key)?.label}`);
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

  // Callback after config save: refresh premium link data and collapse form
  const handleConfigSaved = (key: ChannelKey) => {
    invalidateLink(key);
    setExpandedChannel(null);
  };

  // Check if any channels are available at all
  const anyAvailable = channelConfig
    ? visibleChannels.length > 0 &&
      visibleChannels.some((ch) => channelStates[ch.key] !== 'unavailable')
    : true; // Don't show empty state while loading

  // Separate channels into selectable (configured/active) and non-selectable
  const selectableChannels = visibleChannels.filter(
    (ch) => channelStates[ch.key] === 'configured' || channelStates[ch.key] === 'active',
  );
  const nonSelectableChannels = visibleChannels.filter(
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
              No messaging channels have been configured yet.
              Contact your administrator to enable Telegram or iMessage.
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
                    state={channelStates[key] ?? 'unavailable'}
                    isExpanded={expandedChannel === key}
                    isSwitching={switchingChannel === key}
                    isMutating={toggleMutation.isPending}
                    onActivate={() => handleActivateChannel(key)}
                    onToggleExpand={() => handleToggleExpand(key)}
                    isPremium={isPremium}
                    channelConfig={channelConfig}
                    botInfo={key === 'telegram' ? botInfo : null}
                    telegramLinkData={key === 'telegram' ? telegramLinkData : null}
                    premiumLinkData={linkDataMap[key] ?? null}
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
              state={channelStates[key] ?? 'unavailable'}
              isExpanded={expandedChannel === key}
              isSwitching={false}
              isMutating={toggleMutation.isPending}
              onActivate={() => {}}
              onToggleExpand={() => handleToggleExpand(key)}
              isPremium={isPremium}
              channelConfig={channelConfig}
              botInfo={key === 'telegram' ? botInfo : null}
              telegramLinkData={key === 'telegram' ? telegramLinkData : null}
              premiumLinkData={linkDataMap[key] ?? null}
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
  channelConfig: ChannelStatesResult['channelConfig'];
  botInfo: TelegramBotInfo | null;
  telegramLinkData: TelegramLinkData | null;
  premiumLinkData: PremiumLinkData | null;
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
  premiumLinkData,
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
            premiumLinkData={premiumLinkData}
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
                premiumLinkData={premiumLinkData}
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
  if (key === 'telegram') return 'Contact your administrator to enable Telegram.';
  if (key === 'linq' || key === 'bluebubbles') {
    return 'Contact your administrator to enable iMessage.';
  }
  return '';
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
  // linq and bluebubbles both render as the unified iMessage chat bubble.
  return (
    <svg className="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
    </svg>
  );
}
