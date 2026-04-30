import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Field from '@/components/ui/field';
import TextAssistantCard from '@/components/TextAssistantCard';
import { toast } from '@/lib/toast';
import {
  useChannelConfig,
  useToggleChannelRoute,
  useChannelRoutes,
  useUpdateChannelConfig,
} from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { useIsMobile } from '@/hooks/useIsMobile';
import { getVisibleChannels, isServerAvailable, type ChannelKey } from '@/lib/channel-utils';
import { ChannelConfigForm, type TelegramLinkData, type PremiumLinkData } from '@/components/ChannelConfigForm';
import { normalizeUsPhone, isValidE164, PHONE_FORMAT_ERROR } from '@/lib/phone';
import api from '@/api';

type Selection = ChannelKey | 'none';

export default function GetStartedPage() {
  const isMobile = useIsMobile();
  const navigate = useNavigate();
  const { isPremium } = useAuth();
  const { data: channelConfig } = useChannelConfig();
  const { data: routesData } = useChannelRoutes();
  const visibleChannels = getVisibleChannels(channelConfig);
  const toggleChannelRoute = useToggleChannelRoute();
  const [selectedChannel, setSelectedChannel] = useState<Selection | null>(null);
  const [confirmedChannel, setConfirmedChannel] = useState<Selection | null>(null);

  // Premium link data (fetched once, same pattern as ChannelsPage)
  const [telegramLinkData, setTelegramLinkData] = useState<TelegramLinkData | null>(null);
  const [linkDataMap, setLinkDataMap] = useState<Partial<Record<ChannelKey, PremiumLinkData | null>>>({});

  useEffect(() => {
    if (isPremium) {
      api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      const fetchers: Partial<Record<ChannelKey, () => Promise<{ phone_number: string | null; connected: boolean }>>> = {
        linq: () => api.getLinqLink(),
        bluebubbles: () => api.getBlueBubblesLink(),
      };
      for (const [key, fetcher] of Object.entries(fetchers)) {
        fetcher().then((data) => {
          setLinkDataMap((prev) => ({ ...prev, [key]: { identifier: data.phone_number, connected: data.connected } }));
        }).catch(() => {});
      }
    }
  }, [isPremium]);

  const routes = routesData?.routes ?? [];

  const linqConfigured = channelConfig ? isServerAvailable('linq', channelConfig) : false;
  const fromNumber = channelConfig?.linq_from_number ?? '';
  const bbAddress = channelConfig?.bluebubbles_imessage_address ?? '';
  const bbConfigured = channelConfig ? isServerAvailable('bluebubbles', channelConfig) : false;
  const telegramConfigured = channelConfig ? isServerAvailable('telegram', channelConfig) : false;
  const [telegramBotInfo, setTelegramBotInfo] = useState<{ bot_username: string; bot_link: string } | null>(null);
  useEffect(() => {
    if (telegramConfigured) {
      api.getTelegramBotInfo().then(setTelegramBotInfo).catch(() => {});
    }
  }, [telegramConfigured]);
  const imessageNumber = linqConfigured && fromNumber
    ? fromNumber
    : (bbConfigured && bbAddress ? bbAddress : '');
  const imessageBackend: ChannelKey | null = linqConfigured ? 'linq' : (bbConfigured ? 'bluebubbles' : null);

  // Find the currently active channel route
  const activeChannelKey = visibleChannels.find(
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

  const handleSelectChannel = useCallback((channel: Selection) => {
    setSelectedChannel(channel);

    if (channel === 'none') {
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
  }, [activeChannelKey, confirmedChannel, toggleChannelRoute]);

  const handleConfigSaved = (key: ChannelKey) => {
    if (isPremium) {
      if (key === 'telegram') api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      const fetchers: Partial<Record<ChannelKey, () => Promise<{ phone_number: string | null; connected: boolean }>>> = {
        linq: () => api.getLinqLink(),
        bluebubbles: () => api.getBlueBubblesLink(),
      };
      const fetcher = fetchers[key];
      if (fetcher) {
        fetcher().then((data) => {
          setLinkDataMap((prev) => ({ ...prev, [key]: { identifier: data.phone_number, connected: data.connected } }));
        }).catch(() => {});
      }
    }
  };

  const handleDismiss = () => {
    try { sessionStorage.setItem('getStartedDismissed', '1'); } catch { /* ignore */ }
    navigate(selectedChannel && selectedChannel !== 'none' ? '/app/dashboard' : '/app/chat', {
      replace: true,
    });
  };

  if (isMobile) {
    return (
      <MobileGetStarted
        channelConfig={channelConfig}
        imessageBackend={imessageBackend}
        imessageNumber={imessageNumber}
        telegramConfigured={telegramConfigured}
        telegramBotInfo={telegramBotInfo}
        telegramLinkData={telegramLinkData}
        isPremium={isPremium}
        onDismiss={handleDismiss}
        onActivateRoute={(key) => handleSelectChannel(key)}
      />
    );
  }

  // Determine Step 2 heading based on selection
  const step2Label = selectedChannel === 'none'
    ? 'No setup needed'
    : selectedChannel
      ? `Configure ${visibleChannels.find((c) => c.key === selectedChannel)?.label ?? selectedChannel}`
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
                {visibleChannels.map(({ key, label }) => (
                  <ChannelRadioItem
                    key={key}
                    value={key}
                    label={label}
                    isSelected={selectedChannel === key}
                    isConfirmed={confirmedChannel === key}
                    isSwitching={toggleChannelRoute.isPending && selectedChannel === key && confirmedChannel !== key}
                    isMutating={toggleChannelRoute.isPending}
                    onSelect={() => handleSelectChannel(key)}
                  />
                ))}

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
                {visibleChannels.length === 0 && (
                  <p className="text-xs text-muted-foreground italic px-3 py-2">
                    No messaging channels are configured on the server yet.
                    Use web chat for now, or ask your admin to enable iMessage or Telegram.
                  </p>
                )}
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
                    premiumLinkData={linkDataMap[selectedChannel] ?? null}
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
              {selectedChannel === 'linq' && linqConfigured && fromNumber ? (
                <div className="mt-2 grid gap-2">
                  <TextAssistantCard
                    fromNumber={fromNumber}
                    subtitle="Send an iMessage to this address to get started."
                    qrSize={120}
                  />
                  <PhotoAccessHint />
                </div>
              ) : selectedChannel === 'bluebubbles' && bbConfigured && bbAddress ? (
                <div className="mt-2 grid gap-2">
                  <TextAssistantCard
                    fromNumber={bbAddress}
                    subtitle="Send an iMessage to this address to get started."
                    qrSize={120}
                  />
                  <PhotoAccessHint />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground mt-1">
                  {selectedChannel === 'none'
                    ? 'Use the chat in the sidebar to talk to your assistant.'
                    : selectedChannel === 'telegram'
                      ? 'Open Telegram and send a message to your bot to get started.'
                      : selectedChannel === 'linq' || selectedChannel === 'bluebubbles'
                        ? 'Send an iMessage to your assistant to get started.'
                        : (
                            <>
                              No messaging channel is configured yet. You can also{' '}
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
              {selectedChannel && selectedChannel !== 'none' ? (
                <p className="text-sm text-muted-foreground mt-1">
                  That's it. From here, just text your assistant directly.
                  Clawbolt learns about you and your business as you chat,
                  and you can set up integrations, approve tool access, and
                  adjust settings all from the conversation.
                </p>
              ) : (
                <p className="text-sm text-muted-foreground mt-1">
                  That's it. Clawbolt learns about you and your business as you chat.
                  You can always fine-tune settings later from the sidebar.
                </p>
              )}
            </div>
          </div>
        </Card>
      </div>

      <div className="mt-8 flex justify-center">
        <Button
          variant="primary"
          onClick={handleDismiss}
        >
          {selectedChannel && selectedChannel !== 'none'
            ? 'Got it, take me to the dashboard'
            : 'Got it, take me to chat'}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mobile single-screen layout
// ---------------------------------------------------------------------------

interface MobileProps {
  channelConfig: ReturnType<typeof useChannelConfig>['data'];
  imessageBackend: ChannelKey | null;
  imessageNumber: string;
  telegramConfigured: boolean;
  telegramBotInfo: { bot_username: string; bot_link: string } | null;
  telegramLinkData: TelegramLinkData | null;
  isPremium: boolean;
  onDismiss: () => void;
  onActivateRoute: (key: ChannelKey) => void;
}

function MobileGetStarted(props: MobileProps) {
  const navigate = useNavigate();
  const {
    channelConfig,
    imessageBackend,
    imessageNumber,
    telegramConfigured,
    onDismiss,
  } = props;

  const imessageAvailable = imessageBackend !== null && imessageNumber !== '';
  const telegramAvailable = telegramConfigured;
  const eitherAvailable = imessageAvailable || telegramAvailable;

  // Derive activeChannel rather than seeding it from props at mount. The
  // useState initializer runs once and would lock in 'telegram' if
  // channelConfig hadn't loaded yet (imessageAvailable=false at that
  // moment). Pattern: store only the user's explicit choice; fall back
  // to the natural default ("iMessage if available, else Telegram"),
  // which re-evaluates as channelConfig loads.
  const [userChannelChoice, setUserChannelChoice] = useState<
    'imessage' | 'telegram' | null
  >(null);
  const defaultChannel: 'imessage' | 'telegram' = imessageAvailable
    ? 'imessage'
    : 'telegram';
  const activeChannel = userChannelChoice ?? defaultChannel;

  if (!channelConfig) {
    return (
      <div className="px-4 py-8 max-w-md mx-auto">
        <div className="animate-pulse h-32 bg-panel rounded-md" aria-hidden />
      </div>
    );
  }

  if (!eitherAvailable) {
    return (
      <div className="px-4 py-8 max-w-md mx-auto grid gap-4">
        <h2 className="text-lg font-semibold font-display">Get Started</h2>
        <p className="text-sm text-muted-foreground">
          No messaging channels are configured on the server yet. Use web chat
          for now, or ask your admin to enable iMessage or Telegram.
        </p>
        <Button variant="primary" className="w-full" onClick={() => navigate('/app/chat')}>
          Open web chat
        </Button>
      </div>
    );
  }

  return (
    <div className="px-4 py-6 max-w-md mx-auto grid gap-5">
      <div>
        <h2 className="text-xl font-semibold font-display">Hey, I'm Clawbolt</h2>
        <p className="text-sm text-muted-foreground mt-1">
          {activeChannel === 'imessage'
            ? "Text me to get started. I'm an AI assistant for tradespeople. Tell me your phone number so I know it's you when you message me."
            : "I'm an AI assistant for tradespeople. Open Telegram to start chatting with me."}
        </p>
      </div>

      {imessageAvailable && telegramAvailable && (
        <MobileChannelToggle active={activeChannel} onSelect={setUserChannelChoice} />
      )}

      {activeChannel === 'imessage' ? (
        <MobileImessageFlow {...props} />
      ) : (
        <MobileTelegramFlow {...props} />
      )}

      <button
        type="button"
        className="text-sm text-primary hover:underline self-center"
        onClick={onDismiss}
      >
        Use web chat instead
      </button>
    </div>
  );
}

function MobileChannelToggle({
  active,
  onSelect,
}: {
  active: 'imessage' | 'telegram';
  onSelect: (c: 'imessage' | 'telegram') => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Messaging channel"
      className="grid grid-cols-2 gap-1 p-1 bg-panel rounded-xl"
    >
      {(['imessage', 'telegram'] as const).map((c) => (
        <button
          key={c}
          type="button"
          role="tab"
          aria-selected={active === c}
          onClick={() => onSelect(c)}
          className={`px-3 py-2 text-sm font-medium rounded-lg transition-colors ${
            active === c
              ? 'bg-card text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          {c === 'imessage' ? 'iMessage' : 'Telegram'}
        </button>
      ))}
    </div>
  );
}

function MobileImessageFlow({
  imessageBackend,
  imessageNumber,
  isPremium,
  onActivateRoute,
}: MobileProps) {
  const updateChannelConfig = useUpdateChannelConfig();
  const [phone, setPhone] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [linked, setLinked] = useState(false);
  const [copied, setCopied] = useState(false);

  // BlueBubbles can be configured with either a phone number or an iCloud
  // email. An ``sms:user@icloud.com`` deep-link is malformed and most OS
  // handlers reject it, so the email shape gets a copy-the-address UX
  // instead.
  const imessageIsEmail = imessageNumber.includes('@');

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(imessageNumber);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API may be blocked; the user can long-press to copy.
    }
  };

  const handleStart = async () => {
    setError(null);
    if (!imessageBackend) return;
    const normalized = normalizeUsPhone(phone);
    if (!isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    setSaving(true);
    try {
      if (isPremium) {
        if (imessageBackend === 'linq') await api.setLinqLink(normalized);
        else await api.setBlueBubblesLink(normalized);
      } else {
        // OSS: persist the phone to the server-level allowed list so the
        // backend will route the user's first inbound back to this account.
        const updates =
          imessageBackend === 'linq'
            ? { linq_allowed_numbers: normalized }
            : { bluebubbles_allowed_numbers: normalized };
        await updateChannelConfig.mutateAsync(updates);
      }
      onActivateRoute(imessageBackend);
      setLinked(true);
      if (!imessageIsEmail) {
        window.location.href = `sms:${imessageNumber}`;
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
      setSaving(false);
    }
  };

  if (linked) {
    return (
      <Card className="p-4 grid gap-3">
        {imessageIsEmail ? (
          <>
            <div className="text-sm">
              Saved. Open Messages on your iCloud-connected device and send a
              note to this address to get started.
            </div>
            <button
              type="button"
              onClick={onCopy}
              className="text-center text-sm font-mono py-2 rounded-md hover:bg-secondary-hover focus:outline-none focus:ring-2 focus:ring-primary/30"
              aria-label={`Copy address ${imessageNumber}`}
            >
              {imessageNumber}
              <span className="ml-2 text-xs text-muted-foreground">
                {copied ? '(copied)' : '(tap to copy)'}
              </span>
            </button>
          </>
        ) : (
          <>
            <div className="text-sm">
              Saved. If your Messages app didn't open, tap the button below.
            </div>
            <a href={`sms:${imessageNumber}`} className="block">
              <Button variant="primary" className="w-full">Open Messages</Button>
            </a>
            <p className="text-center text-sm font-mono">{imessageNumber}</p>
          </>
        )}
        <PhotoAccessHint />
      </Card>
    );
  }

  return (
    <>
      <Field label="Your phone number">
        <Input
          value={phone}
          onChange={(e) => {
            setPhone(e.target.value);
            if (error) setError(null);
          }}
          placeholder="(555) 123-4567"
          inputMode="tel"
          autoComplete="tel"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'mobile-phone-error' : undefined}
        />
        {error ? (
          <p id="mobile-phone-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">
            US numbers default to +1. Type a leading + for other countries.
          </p>
        )}
      </Field>

      <Button
        variant="primary"
        className="w-full"
        isLoading={saving}
        disabled={saving || !phone.trim()}
        onClick={handleStart}
      >
        Text Clawbolt
      </Button>
    </>
  );
}

function MobileTelegramFlow({
  telegramBotInfo,
  telegramLinkData,
  isPremium,
  onActivateRoute,
}: MobileProps) {
  const updateChannelConfig = useUpdateChannelConfig();
  const [telegramId, setTelegramId] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [linked, setLinked] = useState(false);

  const initialId = telegramLinkData?.telegram_user_id ?? '';
  const displayedId = telegramId || initialId;

  const handleStart = async () => {
    setError(null);
    const trimmed = displayedId.trim();
    // Telegram numeric user IDs are positive integers; in practice 9-12
    // digits today but Telegram has hinted they may grow. Validate as
    // digits only with a sane lower bound.
    if (!/^\d{6,15}$/.test(trimmed)) {
      setError('Use your numeric Telegram user ID (digits only).');
      return;
    }
    setSaving(true);
    try {
      if (isPremium) {
        await api.setTelegramLink(trimmed);
      } else {
        await updateChannelConfig.mutateAsync({ telegram_allowed_chat_id: trimmed });
      }
      onActivateRoute('telegram');
      setLinked(true);
      // Open the Telegram bot in the app if installed; web fallback.
      if (telegramBotInfo?.bot_link) {
        window.location.href = telegramBotInfo.bot_link;
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
      setSaving(false);
    }
  };

  if (linked) {
    return (
      <Card className="p-4 grid gap-3">
        <div className="text-sm">
          Saved. If Telegram didn't open, tap the button below.
        </div>
        {telegramBotInfo?.bot_link ? (
          <a href={telegramBotInfo.bot_link} className="block">
            <Button variant="primary" className="w-full">
              Open Telegram
            </Button>
          </a>
        ) : (
          <p className="text-xs text-muted-foreground">
            Search for the Clawbolt bot in Telegram and send your first
            message.
          </p>
        )}
        {telegramBotInfo?.bot_username && (
          <p className="text-center text-sm font-mono">@{telegramBotInfo.bot_username}</p>
        )}
        <PhotoAccessHint />
      </Card>
    );
  }

  return (
    <>
      <Field label="Your Telegram user ID">
        <Input
          value={displayedId}
          onChange={(e) => {
            setTelegramId(e.target.value);
            if (error) setError(null);
          }}
          placeholder="123456789"
          inputMode="numeric"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'mobile-telegram-error' : 'mobile-telegram-help'}
        />
        {error ? (
          <p id="mobile-telegram-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p id="mobile-telegram-help" className="text-xs text-muted-foreground mt-1">
            Your numeric Telegram user ID. Send /start to @userinfobot on
            Telegram to find it.
          </p>
        )}
      </Field>

      <Button
        variant="primary"
        className="w-full"
        isLoading={saving}
        disabled={saving || !displayedId.trim()}
        onClick={handleStart}
      >
        Open Telegram
      </Button>
    </>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

function PhotoAccessHint() {
  return (
    <p className="text-xs text-muted-foreground italic">
      Clawbolt only sees photos you send it. It can't browse your camera roll.
    </p>
  );
}

function ChannelRadioItem({
  value,
  label,
  description,
  isSelected,
  isConfirmed,
  isSwitching,
  isMutating,
  onSelect,
}: {
  value: string;
  label: string;
  description?: string;
  isSelected: boolean;
  isConfirmed: boolean;
  isSwitching: boolean;
  isMutating: boolean;
  onSelect: () => void;
}) {
  return (
    <label
      className={`flex items-center gap-3 p-3 rounded-xl border transition-colors ${
        isSelected
          ? 'border-primary bg-primary-light cursor-pointer'
          : 'border-border hover:border-primary/40 cursor-pointer'
      }`}
    >
      {isSwitching ? (
        <span className="w-4 h-4 shrink-0 flex items-center justify-center" aria-busy="true">
          <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        </span>
      ) : (
        <input
          type="radio"
          name="onboarding-channel"
          value={value}
          checked={isSelected}
          onChange={onSelect}
          disabled={isMutating}
          className="accent-primary w-4 h-4 shrink-0"
        />
      )}
      <div className="flex-1">
        <span className="text-sm font-medium">{label}</span>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
      </div>
      {isConfirmed && (
        <span className="text-xs text-success flex items-center gap-1" aria-label="Confirmed">
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
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
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
    </svg>
  );
}

function RocketIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.63 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.841M3.75 21h.008v.008H3.75V21z" />
    </svg>
  );
}
