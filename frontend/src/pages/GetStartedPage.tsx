import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Field from '@/components/ui/field';
import TextAssistantCard from '@/components/TextAssistantCard';
import DataSharingConsentSection from '@/components/DataSharingConsentSection';
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

// Channels with phone-based outbound that can deliver the onboarding
// welcome text. Telegram is excluded: its bots cannot initiate a
// conversation with a user who hasn't /start-ed them first.
type WelcomeChannel = 'linq' | 'twilio' | 'bluebubbles';

function isWelcomeChannel(channel: Selection | null): channel is WelcomeChannel {
  return channel === 'linq' || channel === 'twilio' || channel === 'bluebubbles';
}

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

  // Tracks per-channel welcome-text outcome from Step 2's save+send. When
  // Step 2 fires the welcome successfully, the timestamp here drives Step
  // 3's DesktopWelcomeStep to remount in its 'sent' state with the resend
  // cooldown ticking. ``failedAt`` flips Step 3 into the legacy
  // "text us yourself" fallback so a delivery error does not leave the
  // user stranded.
  const [welcomeSentAt, setWelcomeSentAt] = useState<Partial<Record<ChannelKey, number>>>({});
  const [welcomeFailedAt, setWelcomeFailedAt] = useState<Partial<Record<ChannelKey, number>>>({});



  useEffect(() => {
    if (isPremium) {
      api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      const fetchers: Partial<Record<ChannelKey, () => Promise<{ phone_number: string | null; connected: boolean }>>> = {
        linq: () => api.getLinqLink(),
        bluebubbles: () => api.getBlueBubblesLink(),
        twilio: () => api.getTwilioLink(),
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
  // Bot's outbound sender for display. Operator-configured in both OSS
  // and premium modes (premium uses a single shared RCS/SMS sender via
  // Messaging Service).
  const twilioAddress = channelConfig?.twilio_phone_number || '';
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

  // Pre-populate selection from active route on initial data load. The
  // desktop one-card flow also auto-picks the first visible channel when
  // nothing is enabled yet so the setup form is in front of the user
  // immediately, mirroring how mobile drops the user straight into the
  // phone-number input.
  const prePopulated = useRef(false);
  useEffect(() => {
    if (prePopulated.current || !channelConfig || !routesData) return;
    prePopulated.current = true;
    if (activeChannelKey) {
      setSelectedChannel(activeChannelKey);
      setConfirmedChannel(activeChannelKey);
    } else if (!isMobile) {
      const first = visibleChannels[0];
      if (first) handleSelectChannel(first.key);
    }
  }, [channelConfig, routesData, activeChannelKey, isMobile, visibleChannels, handleSelectChannel]);

  const handleConfigSaved = (key: ChannelKey) => {
    if (isPremium) {
      if (key === 'telegram') api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      const fetchers: Partial<Record<ChannelKey, () => Promise<{ phone_number: string | null; connected: boolean }>>> = {
        linq: () => api.getLinqLink(),
        bluebubbles: () => api.getBlueBubblesLink(),
        twilio: () => api.getTwilioLink(),
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
        {visibleChannels.length === 0 ? (
          <Card>
            <p className="text-sm text-muted-foreground">
              No messaging channels are configured on the server yet.
              Use web chat for now, or ask your admin to enable iMessage or Telegram.
            </p>
          </Card>
        ) : (
          <Card>
            {visibleChannels.length > 1 && (
              <div
                role="tablist"
                aria-label="Messaging channel"
                className="flex gap-1 p-1 bg-panel rounded-xl mb-4"
              >
                {visibleChannels.map(({ key, label }) => (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    aria-selected={selectedChannel === key}
                    onClick={() => handleSelectChannel(key)}
                    disabled={toggleChannelRoute.isPending}
                    className={`flex-1 px-3 py-2 text-sm font-medium rounded-lg transition-colors ${
                      selectedChannel === key
                        ? 'bg-card text-foreground shadow-sm'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}

            {selectedChannel && selectedChannel !== 'none' && (
              <div className="grid gap-4">
                <ChannelConfigForm
                  channelKey={selectedChannel}
                  isPremium={isPremium}
                  channelConfig={channelConfig}
                  telegramLinkData={telegramLinkData}
                  premiumLinkData={linkDataMap[selectedChannel] ?? null}
                  onSaved={() => handleConfigSaved(selectedChannel)}
                  triggerWelcome={isPremium && isWelcomeChannel(selectedChannel)}
                  onWelcomeSent={(ch) => {
                    setWelcomeSentAt((prev) => ({ ...prev, [ch]: Date.now() }));
                    setWelcomeFailedAt((prev) => ({ ...prev, [ch]: undefined }));
                  }}
                  onWelcomeFailed={(ch) =>
                    setWelcomeFailedAt((prev) => ({ ...prev, [ch]: Date.now() }))
                  }
                />

                {isPremium && isWelcomeChannel(selectedChannel) ? (
                  // ``key`` includes ``welcomeSentAt`` so a successful save
                  // remounts this with ``startInSentState=true`` and the
                  // cooldown ticking from zero. Switching channels via the
                  // toggle also remounts (key includes channel), which
                  // resets a prior channel's 'sent' state.
                  <DesktopWelcomeStep
                    key={`${selectedChannel}-${welcomeSentAt[selectedChannel] ?? 0}-${welcomeFailedAt[selectedChannel] ?? 0}`}
                    channel={selectedChannel}
                    destination={linkDataMap[selectedChannel]?.identifier ?? ''}
                    isConnected={linkDataMap[selectedChannel]?.connected ?? false}
                    startInSentState={Boolean(welcomeSentAt[selectedChannel])}
                    startInFailedState={Boolean(welcomeFailedAt[selectedChannel])}
                    fallbackAddress={
                      selectedChannel === 'linq'
                        ? fromNumber
                        : selectedChannel === 'bluebubbles'
                          ? bbAddress
                          : twilioAddress
                    }
                  />
                ) : selectedChannel === 'linq' && linqConfigured && fromNumber ? (
                  <TextAssistantCard
                    fromNumber={fromNumber}
                    subtitle="Send an iMessage to this address to get started."
                    qrSize={120}
                  />
                ) : selectedChannel === 'bluebubbles' && bbConfigured && bbAddress ? (
                  <TextAssistantCard
                    fromNumber={bbAddress}
                    subtitle="Send an iMessage to this address to get started."
                    qrSize={120}
                  />
                ) : selectedChannel === 'twilio' && twilioAddress ? (
                  <TextAssistantCard
                    fromNumber={twilioAddress}
                    subtitle="Text this number to chat with your assistant."
                    qrSize={120}
                  />
                ) : selectedChannel === 'telegram' ? (
                  <p className="text-sm text-muted-foreground">
                    Once saved, open Telegram and send a message to your bot to get started.
                  </p>
                ) : null}
              </div>
            )}
          </Card>
        )}

        {/* Privacy: optional opt-in. First-run is the natural moment to ask;
            the same toggle lives on the User page so people can change their
            mind any time. Default off; the API stamps a timestamp on every
            flip so consent history is reconstructable. */}
        <Card>
          <DataSharingConsentSection variant="compact" />
        </Card>
      </div>

      <div className="mt-8 flex items-center justify-between gap-4">
        <button
          type="button"
          className="text-sm text-primary hover:underline"
          onClick={() => navigate('/app/chat')}
        >
          Use web chat instead
        </button>
        <Button variant="primary" onClick={handleDismiss}>
          {selectedChannel && selectedChannel !== 'none'
            ? 'Got it, take me to the dashboard'
            : 'Got it, take me to chat'}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Desktop Step 3: "Text me to start" + success / resend / fallback states
// ---------------------------------------------------------------------------

function DesktopWelcomeStep({
  channel,
  destination,
  isConnected,
  startInSentState,
  startInFailedState,
  fallbackAddress,
}: {
  channel: WelcomeChannel;
  destination: string;
  isConnected: boolean;
  startInSentState: boolean;
  startInFailedState: boolean;
  fallbackAddress: string;
}) {
  type Status = 'idle' | 'sent' | 'failed';
  // Mirrors the backend WELCOME_COOLDOWN_SECONDS. Surfaced in the UI so
  // a user clicking "Resend" before the backend would accept it sees a
  // disabled button instead of a 429 toast.
  const COOLDOWN_SECONDS = 60;

  const initialStatus: Status = startInFailedState
    ? 'failed'
    : startInSentState
      ? 'sent'
      : 'idle';
  const [status, setStatus] = useState<Status>(initialStatus);
  const [sending, setSending] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(
    startInSentState ? COOLDOWN_SECONDS : 0,
  );

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setTimeout(() => setResendCooldown((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendCooldown]);

  const handleSend = async () => {
    setSending(true);
    try {
      await api.sendWelcomeText(channel);
      setStatus('sent');
      setResendCooldown(COOLDOWN_SECONDS);
    } catch (e) {
      setStatus('failed');
      toast.error(
        e instanceof Error
          ? `${e.message} You can still text us yourself.`
          : 'Could not send the welcome text. You can still text us yourself.',
      );
    } finally {
      setSending(false);
    }
  };

  // Not linked yet: Step 2 is the trigger, so Step 3 just describes
  // what's about to happen. Discoverability fix: previously this branch
  // fell through to the legacy QR card, making the new flow invisible
  // until after a save.
  if (!isConnected) {
    return (
      <div className="mt-3 grid gap-2">
        <p className="text-sm text-muted-foreground">
          Enter your phone number above and click "Save and text me to start".
          We'll text you right away and you reply from there.
        </p>
      </div>
    );
  }

  if (status === 'sent') {
    return (
      <div className="mt-3 grid gap-3">
        <p className="text-sm">
          We just texted you at <span className="font-mono">{destination}</span>.
          Reply to that message and Clawbolt will take it from there.
        </p>
        <div>
          <Button
            variant="secondary"
            onClick={handleSend}
            isLoading={sending}
            disabled={sending || resendCooldown > 0}
          >
            {resendCooldown > 0 ? `Resend in ${resendCooldown}s` : 'Resend'}
          </Button>
        </div>
      </div>
    );
  }

  if (status === 'failed' && fallbackAddress) {
    return (
      <div className="mt-3 grid gap-2">
        <p className="text-sm text-muted-foreground">
          We couldn't send the welcome text. Send a message to the assistant
          yourself to get started:
        </p>
        <TextAssistantCard
          fromNumber={fallbackAddress}
          subtitle="Text this address to chat with your assistant."
          qrSize={120}
        />
        <div>
          <Button variant="secondary" onClick={handleSend} isLoading={sending} disabled={sending}>
            Try sending again
          </Button>
        </div>
      </div>
    );
  }

  // Linked but no welcome has been sent (e.g. user revisits Get Started
  // after onboarding, or Step 2 saved without firing welcome).
  return (
    <div className="mt-3 grid gap-2">
      <p className="text-sm text-muted-foreground">
        Click the button and Clawbolt will text{' '}
        <span className="font-mono">{destination}</span> to start the conversation.
        Reply to that message to begin.
      </p>
      <div>
        <Button variant="primary" onClick={handleSend} isLoading={sending} disabled={sending}>
          Text me to start
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
        <Card>
          <DataSharingConsentSection variant="compact" />
        </Card>
      </div>
    );
  }

  return (
    <div className="px-4 py-6 max-w-md mx-auto grid gap-5">
      <div>
        <h2 className="text-xl font-semibold font-display">Hey, I'm Clawbolt</h2>
        <p className="text-sm text-muted-foreground mt-1">
          {activeChannel === 'imessage'
            ? "I'm an AI assistant for tradespeople. Enter your phone number and I'll text you to get the conversation started."
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

      <Card>
        <DataSharingConsentSection variant="compact" />
      </Card>

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
  const [userPhone, setUserPhone] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [linked, setLinked] = useState(false);
  const [welcomeSent, setWelcomeSent] = useState(false);
  const [resending, setResending] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(0);
  const [copied, setCopied] = useState(false);

  // Mirrors backend WELCOME_COOLDOWN_SECONDS so the resend button shows a
  // countdown instead of letting the user spam through to a 429 toast.
  const RESEND_COOLDOWN_SECONDS = 60;

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setTimeout(() => setResendCooldown((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendCooldown]);

  // BlueBubbles can be configured with either a phone number or an iCloud
  // email. An ``sms:user@icloud.com`` deep-link is malformed and most OS
  // handlers reject it, so the email shape gets a copy-the-address UX
  // instead. (Welcome-text uses the same identifier shape; the destination
  // text is delivered to the user's iCloud account either way.)
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
      setUserPhone(normalized);
      setLinked(true);

      if (isPremium && isWelcomeChannel(imessageBackend)) {
        try {
          await api.sendWelcomeText(imessageBackend);
          setWelcomeSent(true);
          setResendCooldown(RESEND_COOLDOWN_SECONDS);
        } catch (e) {
          // Fall back to the deep-link / copy-address UX so the user can
          // still kick the conversation off themselves.
          toast.error(
            e instanceof Error
              ? `${e.message} You can still text us yourself.`
              : 'Could not send the welcome text. You can still text us yourself.',
          );
          if (!imessageIsEmail) {
            window.location.href = `sms:${imessageNumber}`;
          }
        }
      } else if (!imessageIsEmail) {
        // Non-premium / unsupported backend: keep the legacy deep-link.
        window.location.href = `sms:${imessageNumber}`;
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  const handleResend = async () => {
    if (!isPremium || !isWelcomeChannel(imessageBackend)) return;
    setResending(true);
    try {
      await api.sendWelcomeText(imessageBackend);
      toast.success('Sent again. Check your messages.');
      setResendCooldown(RESEND_COOLDOWN_SECONDS);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Could not resend.');
    } finally {
      setResending(false);
    }
  };

  if (welcomeSent) {
    return (
      <Card className="p-4 grid gap-3">
        <div className="text-sm">
          We just texted you at <span className="font-mono">{userPhone}</span>.
          Reply to that message to start chatting with Clawbolt.
        </div>
        <Button
          variant="secondary"
          className="w-full"
          isLoading={resending}
          disabled={resending || resendCooldown > 0}
          onClick={handleResend}
        >
          {resendCooldown > 0 ? `Resend in ${resendCooldown}s` : 'Resend'}
        </Button>
      </Card>
    );
  }

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
        {isPremium && isWelcomeChannel(imessageBackend) ? 'Text me to start' : 'Text Clawbolt'}
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

