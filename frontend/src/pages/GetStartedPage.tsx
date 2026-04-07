import { useState, useEffect, useRef } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import TextAssistantCard from '@/components/TextAssistantCard';
import { toast } from '@/lib/toast';
import { useUpdateProfile, useChannelConfig, useToggleChannelRoute, useChannelRoutes } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { MESSAGING_CHANNELS, isServerAvailable, type ChannelKey } from '@/lib/channel-utils';
import { ChannelConfigForm, type TelegramLinkData, type LinqLinkData, type BlueBubblesLinkData } from '@/components/ChannelConfigForm';
import api from '@/api';
import type { AppShellContext } from '@/layouts/AppShell';

type Selection = ChannelKey | 'none';

export default function GetStartedPage() {
  const { reloadProfile } = useOutletContext<AppShellContext>();
  const navigate = useNavigate();
  const { isPremium } = useAuth();
  const updateProfile = useUpdateProfile();
  const { data: channelConfig } = useChannelConfig();
  const { data: routesData } = useChannelRoutes();
  const toggleChannelRoute = useToggleChannelRoute();
  const [selectedChannel, setSelectedChannel] = useState<Selection | null>(null);
  const [confirmedChannel, setConfirmedChannel] = useState<Selection | null>(null);

  // Premium link data (fetched once, same pattern as ChannelsPage)
  const [telegramLinkData, setTelegramLinkData] = useState<TelegramLinkData | null>(null);
  const [linqLinkData, setLinqLinkData] = useState<LinqLinkData | null>(null);
  const [blueBubblesLinkData, setBlueBubblesLinkData] = useState<BlueBubblesLinkData | null>(null);

  useEffect(() => {
    if (isPremium) {
      api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      api.getLinqLink().then(setLinqLinkData).catch(() => {});
      api.getBlueBubblesLink().then(setBlueBubblesLinkData).catch(() => {});
    }
  }, [isPremium]);

  const routes = routesData?.routes ?? [];

  const linqConfigured = channelConfig ? isServerAvailable('linq', channelConfig) : false;
  const fromNumber = channelConfig?.linq_from_number ?? '';
  const bbAddress = channelConfig?.bluebubbles_imessage_address ?? '';
  const bbConfigured = channelConfig ? isServerAvailable('bluebubbles', channelConfig) : false;

  // Find the currently active channel route
  const activeChannelKey = MESSAGING_CHANNELS.find(
    (ch) => routes.some((r) => r.channel === ch.key && r.enabled),
  )?.key ?? null;

  // Pre-populate selection from active route on initial data load
  const prePopulated = useRef(false);
  useEffect(() => {
    if (prePopulated.current || !channelConfig || !routesData) return;
    prePopulated.current = true;
    if (activeChannelKey) {
      setSelectedChannel(activeChannelKey);
      setConfirmedChannel(activeChannelKey);
    }
  }, [channelConfig, routesData, activeChannelKey]);

  const handleSelectChannel = (channel: Selection) => {
    setSelectedChannel(channel);

    if (channel === 'none') {
      // Disable the currently active channel
      const toDisable = confirmedChannel && confirmedChannel !== 'none'
        ? confirmedChannel
        : activeChannelKey;

      if (toDisable) {
        toggleChannelRoute.mutate(
          { channel: toDisable, enabled: false },
          {
            onSuccess: () => setConfirmedChannel('none'),
            onError: (e) => {
              setSelectedChannel(confirmedChannel);
              toast.error(e.message);
            },
          },
        );
      } else {
        setConfirmedChannel('none');
      }
      return;
    }

    // Enable the selected channel route (backend auto-disables others)
    toggleChannelRoute.mutate(
      { channel, enabled: true },
      {
        onSuccess: () => setConfirmedChannel(channel),
        onError: (e) => {
          setSelectedChannel(confirmedChannel);
          toast.error(e.message);
        },
      },
    );
  };

  const handleConfigSaved = (key: ChannelKey) => {
    // Refresh premium link data after save
    if (isPremium) {
      if (key === 'telegram') api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      if (key === 'linq') api.getLinqLink().then(setLinqLinkData).catch(() => {});
    }
  };

  const handleDismiss = () => {
    updateProfile.mutate(
      { onboarding_complete: true },
      {
        onSuccess: () => {
          reloadProfile();
          navigate('/app/chat', { replace: true });
        },
        onError: (e) => toast.error(e.message),
      },
    );
  };

  // Determine Step 2 heading based on selection
  const step2Label = selectedChannel === 'none'
    ? 'No setup needed'
    : selectedChannel
      ? `Configure ${MESSAGING_CHANNELS.find((c) => c.key === selectedChannel)?.label ?? selectedChannel}`
      : 'Configure your channel';

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-8">
        <h2 className="text-xl font-semibold font-display">Get Started</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Clawbolt is your AI assistant for the trades. Choose how you want to message
          your assistant and you'll be up and running in minutes.
        </p>
      </div>

      <div className="grid gap-4">
        {/* Step 1: Choose messaging channel */}
        <Card>
          <div className="flex items-start gap-4">
            <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
              <ChannelIcon />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-muted-foreground">Step 1</span>
              </div>
              <h3 className="text-sm font-semibold font-display">Choose your messaging channel</h3>
              <p className="text-sm text-muted-foreground mt-1">
                Pick how you want to talk to Clawbolt. You can change this later.
              </p>
              <div className="mt-3 grid gap-2" role="radiogroup" aria-label="Messaging channel">
                {MESSAGING_CHANNELS.map(({ key, label }) => {
                  const configured = channelConfig ? isServerAvailable(key, channelConfig) : false;
                  return (
                    <ChannelRadioItem
                      key={key}
                      value={key}
                      label={label}
                      isSelected={selectedChannel === key}
                      isConfirmed={confirmedChannel === key}
                      isDisabled={!configured}
                      isSwitching={toggleChannelRoute.isPending && selectedChannel === key && confirmedChannel !== key}
                      isMutating={toggleChannelRoute.isPending}
                      onSelect={() => handleSelectChannel(key)}
                    />
                  );
                })}

                <ChannelRadioItem
                  value="none"
                  label="None"
                  description="Web chat only, no external messaging channel"
                  isSelected={selectedChannel === 'none'}
                  isConfirmed={confirmedChannel === 'none'}
                  isSwitching={toggleChannelRoute.isPending && selectedChannel === 'none' && confirmedChannel !== 'none'}
                  isMutating={toggleChannelRoute.isPending}
                  onSelect={() => handleSelectChannel('none')}
                />
              </div>
            </div>
          </div>
        </Card>

        {/* Step 2: Channel-specific setup */}
        <Card>
          <div className="flex items-start gap-4">
            <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
              <SettingsIcon />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-muted-foreground">Step 2</span>
              </div>
              <h3 className="text-sm font-semibold font-display">{step2Label}</h3>
              {selectedChannel === 'none' ? (
                <p className="text-sm text-muted-foreground mt-1">
                  You can always add a messaging channel later from the{' '}
                  <button type="button" className="text-primary hover:underline font-medium" onClick={() => navigate('/app/channels')}>
                    Channels page
                  </button>
                  .
                </p>
              ) : selectedChannel ? (
                <div className="mt-3">
                  <ChannelConfigForm
                    channelKey={selectedChannel}
                    isPremium={isPremium}
                    channelConfig={channelConfig}
                    telegramLinkData={telegramLinkData}
                    linqLinkData={linqLinkData}
                    blueBubblesLinkData={blueBubblesLinkData}
                    onSaved={() => handleConfigSaved(selectedChannel)}
                  />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground mt-1">
                  Select a channel above to configure it.
                </p>
              )}
            </div>
          </div>
        </Card>

        {/* Step 3: Send a message */}
        <Card>
          <div className="flex items-start gap-4">
            <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
              <ChatIcon />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-muted-foreground">Step 3</span>
              </div>
              <h3 className="text-sm font-semibold font-display">Send a message</h3>
              {linqConfigured && fromNumber && selectedChannel === 'linq' ? (
                <div className="mt-2">
                  <TextAssistantCard
                    fromNumber={fromNumber}
                    subtitle="Just say hello to get started."
                    qrSize={80}
                  />
                </div>
              ) : bbConfigured && bbAddress && selectedChannel === 'bluebubbles' ? (
                <div className="mt-2">
                  <TextAssistantCard
                    fromNumber={bbAddress}
                    subtitle="Send an iMessage to this address to get started."
                    qrSize={80}
                  />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground mt-1">
                  {selectedChannel === 'none'
                    ? 'Use the chat in the sidebar to talk to your assistant.'
                    : selectedChannel === 'telegram'
                      ? 'Open Telegram and send a message to your bot to get started.'
                      : selectedChannel === 'bluebubbles'
                        ? 'Send an iMessage to get started.'
                        : linqConfigured && fromNumber
                          ? 'Text your assistant to get started.'
                          : (
                              <>
                                Text messaging is not configured yet. You can also{' '}
                                <button
                                  type="button"
                                  className="text-primary hover:underline font-medium"
                                  onClick={() => navigate('/app/chat')}
                                >
                                  chat from the web
                                </button>
                                {' '}or{' '}
                                <button
                                  type="button"
                                  className="text-primary hover:underline font-medium"
                                  onClick={() => navigate('/app/channels')}
                                >
                                  set up a channel
                                </button>
                                .
                              </>
                            )}
                </p>
              )}
            </div>
          </div>
        </Card>

        {/* Step 4: You're off to the races */}
        <Card>
          <div className="flex items-start gap-4">
            <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
              <RocketIcon />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-muted-foreground">Step 4</span>
              </div>
              <h3 className="text-sm font-semibold font-display">You're off to the races</h3>
              <p className="text-sm text-muted-foreground mt-1">
                That's it. Clawbolt learns about you and your business as you chat.
                You can always fine-tune settings later from the sidebar.
              </p>
            </div>
          </div>
        </Card>
      </div>

      <div className="mt-8 flex justify-center">
        <Button
          variant="primary"
          onClick={handleDismiss}
          disabled={updateProfile.isPending}
          isLoading={updateProfile.isPending}
        >
          Got it, take me to chat
        </Button>
      </div>
    </div>
  );
}

function ChannelRadioItem({
  value,
  label,
  description,
  isSelected,
  isConfirmed,
  isDisabled,
  isSwitching,
  isMutating,
  onSelect,
}: {
  value: string;
  label: string;
  description?: string;
  isSelected: boolean;
  isConfirmed: boolean;
  isDisabled?: boolean;
  isSwitching: boolean;
  isMutating: boolean;
  onSelect: () => void;
}) {
  return (
    <label
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
          name="onboarding-channel"
          value={value}
          checked={isSelected}
          onChange={onSelect}
          disabled={isDisabled || isMutating}
          className="accent-primary w-4 h-4 shrink-0"
        />
      )}
      <div className="flex-1">
        <span className="text-sm font-medium">{label}</span>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
      </div>
      {isConfirmed && (
        <span className="text-xs text-success flex items-center gap-1">
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
          </svg>
        </span>
      )}
    </label>
  );
}

// --- Step icons (inline SVG) ---

function ChannelIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
    </svg>
  );
}

function RocketIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.63 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.841M3.75 21h.008v.008H3.75V21z" />
    </svg>
  );
}
