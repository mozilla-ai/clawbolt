import { useState } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Field from '@/components/ui/field';
import TextAssistantCard from '@/components/TextAssistantCard';
import { toast } from '@/lib/toast';
import { useUpdateProfile, useChannelConfig, useUpdateChannelConfig, useToggleChannelRoute } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { getAccessToken } from '@/lib/api-client';
import type { AppShellContext } from '@/layouts/AppShell';

const CHANNEL_OPTIONS = [
  { key: 'linq', label: 'Text Messaging', description: 'iMessage, RCS, or SMS from your phone' },
  { key: 'telegram', label: 'Telegram', description: 'Message via the Telegram app' },
  { key: 'bluebubbles', label: 'BlueBubbles', description: 'iMessage via self-hosted Mac bridge' },
] as const;

type ChannelKey = (typeof CHANNEL_OPTIONS)[number]['key'];

async function setLinqLink(phoneNumber: string): Promise<void> {
  const token = getAccessToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch('/api/channels/linq', {
    method: 'PUT',
    headers,
    body: JSON.stringify({ phone_number: phoneNumber }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string };
    throw new Error(body.detail || `Failed to save: ${res.status}`);
  }
}

export default function GetStartedPage() {
  const { reloadProfile } = useOutletContext<AppShellContext>();
  const navigate = useNavigate();
  const { isPremium } = useAuth();
  const updateProfile = useUpdateProfile();
  const { data: channelConfig } = useChannelConfig();
  const updateChannelConfig = useUpdateChannelConfig();
  const toggleChannelRoute = useToggleChannelRoute();
  const [selectedChannel, setSelectedChannel] = useState<ChannelKey | null>(null);
  const [phoneNumber, setPhoneNumber] = useState('');
  const [phoneSaved, setPhoneSaved] = useState(false);
  const [savingPhone, setSavingPhone] = useState(false);

  const linqConfigured = channelConfig?.linq_api_token_set ?? false;
  const fromNumber = channelConfig?.linq_from_number ?? '';

  const handleSelectChannel = (channel: ChannelKey) => {
    setSelectedChannel(channel);
    // Enable the selected channel route (backend auto-disables others)
    toggleChannelRoute.mutate(
      { channel, enabled: true },
      { onError: (e) => toast.error(e.message) },
    );
  };

  const handleSavePhone = async () => {
    const trimmed = phoneNumber.trim();
    if (!trimmed) {
      toast.error('Please enter your phone number');
      return;
    }

    if (isPremium) {
      setSavingPhone(true);
      try {
        await setLinqLink(trimmed);
        updateProfile.mutate({ phone: trimmed });
        setPhoneSaved(true);
        toast.success('Phone number saved');
      } catch (e) {
        toast.error(e instanceof Error ? e.message : 'Failed to save');
      } finally {
        setSavingPhone(false);
      }
      return;
    }

    updateChannelConfig.mutate(
      { linq_allowed_numbers: trimmed },
      {
        onSuccess: () => {
          updateProfile.mutate({ phone: trimmed });
          setPhoneSaved(true);
          toast.success('Phone number saved');
        },
        onError: (e) => toast.error(e.message),
      },
    );
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
                {CHANNEL_OPTIONS.map(({ key, label, description }) => {
                  const isSelected = selectedChannel === key;
                  return (
                    <label
                      key={key}
                      className={`flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-colors ${
                        isSelected
                          ? 'border-primary bg-primary-light'
                          : 'border-border hover:border-primary/40'
                      }`}
                    >
                      <input
                        type="radio"
                        name="onboarding-channel"
                        value={key}
                        checked={isSelected}
                        onChange={() => handleSelectChannel(key)}
                        disabled={toggleChannelRoute.isPending}
                        className="accent-primary w-4 h-4 shrink-0"
                      />
                      <div className="flex-1">
                        <span className="text-sm font-medium">{label}</span>
                        <p className="text-xs text-muted-foreground">{description}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          </div>
        </Card>

        {/* Step 2: Channel-specific setup */}
        <Card>
          <div className="flex items-start gap-4">
            <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
              <PhoneIcon />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-muted-foreground">Step 2</span>
              </div>
              {selectedChannel === 'linq' || !selectedChannel ? (
                <>
                  <h3 className="text-sm font-semibold font-display">Enter your phone number</h3>
                  <p className="text-sm text-muted-foreground mt-1">
                    This is the number you will text Clawbolt from.
                  </p>
                  <div className="mt-3">
                    <Field label="Phone Number">
                      <div className="flex gap-2">
                        <div className="flex-1">
                          <Input
                            value={phoneNumber}
                            onChange={(e) => setPhoneNumber(e.target.value)}
                            placeholder="e.g. +15551234567"
                            inputMode="tel"
                            disabled={phoneSaved}
                          />
                        </div>
                        {!phoneSaved ? (
                          <Button
                            onClick={handleSavePhone}
                            disabled={savingPhone || updateChannelConfig.isPending || !phoneNumber.trim()}
                            isLoading={savingPhone || updateChannelConfig.isPending}
                          >
                            Save
                          </Button>
                        ) : (
                          <Button variant="ghost" onClick={() => setPhoneSaved(false)}>
                            Edit
                          </Button>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">
                        Use E.164 format with country code (e.g. +1 for US).
                      </p>
                    </Field>
                  </div>
                </>
              ) : selectedChannel === 'telegram' ? (
                <>
                  <h3 className="text-sm font-semibold font-display">Set up Telegram</h3>
                  <p className="text-sm text-muted-foreground mt-1">
                    You can configure your Telegram connection from the{' '}
                    <button
                      type="button"
                      className="text-primary hover:underline font-medium"
                      onClick={() => navigate('/app/channels')}
                    >
                      Channels page
                    </button>
                    {' '}after onboarding.
                  </p>
                </>
              ) : (
                <>
                  <h3 className="text-sm font-semibold font-display">Set up BlueBubbles</h3>
                  <p className="text-sm text-muted-foreground mt-1">
                    You can configure BlueBubbles from the{' '}
                    <button
                      type="button"
                      className="text-primary hover:underline font-medium"
                      onClick={() => navigate('/app/channels')}
                    >
                      Channels page
                    </button>
                    {' '}after onboarding.
                  </p>
                </>
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
              ) : (
                <p className="text-sm text-muted-foreground mt-1">
                  {selectedChannel === 'telegram'
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

// --- Step icons (inline SVG) ---

function ChannelIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z" />
    </svg>
  );
}

function PhoneIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z" />
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
