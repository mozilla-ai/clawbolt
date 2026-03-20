import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import { Divider } from '@heroui/divider';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { getAccessToken } from '@/lib/api-client';
import type { AppShellContext } from '@/layouts/AppShell';

export default function ChannelsPage() {
  const { profile } = useOutletContext<AppShellContext>();

  if (!profile) return null;

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Channels</h2>
      <TelegramSection profile={profile} />
    </div>
  );
}

// --- Premium Telegram linking helpers ---

interface TelegramLinkData {
  telegram_user_id: string | null;
  connected: boolean;
}

function _authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getTelegramLink(): Promise<TelegramLinkData> {
  const res = await fetch('/api/channels/telegram', { headers: _authHeaders() });
  if (!res.ok) throw new Error('Failed to fetch Telegram link');
  return res.json() as Promise<TelegramLinkData>;
}

async function setTelegramLink(telegramUserId: string): Promise<TelegramLinkData> {
  const res = await fetch('/api/channels/telegram', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ..._authHeaders() },
    body: JSON.stringify({ telegram_user_id: telegramUserId }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string };
    throw new Error(body.detail || `Failed to save: ${res.status}`);
  }
  return res.json() as Promise<TelegramLinkData>;
}

// --- Premium Telegram section ---

function PremiumTelegramSection({
  profile,
}: {
  profile: { channel_identifier: string; preferred_channel: string };
}) {
  const connected = !!profile.channel_identifier;
  const [linkData, setLinkData] = useState<TelegramLinkData | null>(null);
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getTelegramLink().then(setLinkData).catch(() => {});
  }, []);

  const displayedId = telegramUserId ?? linkData?.telegram_user_id ?? '';

  const handleSave = async () => {
    if (linkData && displayedId === (linkData.telegram_user_id ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      const result = await setTelegramLink(displayedId);
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
      <Card>
        <h3 className="text-sm font-medium mb-3">Telegram</h3>
        <div className="grid gap-4">
          <Field label="Your Telegram User ID">
            <Input
              value={displayedId}
              onChange={(e) => setTelegramUserId(e.target.value)}
              placeholder="e.g. 123456789"
              inputMode="numeric"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Your numeric Telegram user ID. Send /start to @userinfobot on Telegram to find it.
            </p>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={saving || linkData === null} isLoading={saving}>
              Save
            </Button>
          </div>
        </div>
      </Card>

      <Divider />

      <Card>
        <h3 className="text-sm font-medium mb-3">Connection Status</h3>
        <div className="grid gap-4">
          <Field label="User Connection">
            {connected ? (
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 text-sm">
                  <span className="size-2 rounded-full inline-block shrink-0 bg-success" />
                  Connected
                </span>
                <span className="text-xs text-muted-foreground">
                  Chat ID: {profile.channel_identifier}
                </span>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Save your Telegram user ID above, then send a message to the bot to connect.
              </p>
            )}
          </Field>
          <Field label="Active Channel">
            <p className="text-sm">{profile.preferred_channel || 'webchat'}</p>
          </Field>
        </div>
      </Card>
    </div>
  );
}

// --- OSS Telegram section ---

function OssTelegramSection({
  profile,
}: {
  profile: { channel_identifier: string; preferred_channel: string };
}) {
  const connected = !!profile.channel_identifier;
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? config?.telegram_allowed_chat_id ?? '';

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
        <h3 className="text-sm font-medium mb-3">Telegram</h3>
        <div className="grid gap-4">
          <Field label="Your Telegram User ID">
            <Input
              value={displayedId}
              onChange={(e) => setTelegramUserId(e.target.value)}
              placeholder="e.g. 123456789"
              inputMode="numeric"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Your numeric Telegram user ID. Send /start to @userinfobot on Telegram to find it.
            </p>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>

      <Divider />

      <Card>
        <h3 className="text-sm font-medium mb-3">Connection Status</h3>
        <div className="grid gap-4">
          <Field label="User Connection">
            {connected ? (
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 text-sm">
                  <span className="size-2 rounded-full inline-block shrink-0 bg-success" />
                  Connected
                </span>
                <span className="text-xs text-muted-foreground">
                  Chat ID: {profile.channel_identifier}
                </span>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Send a message to your bot on Telegram to connect.
              </p>
            )}
          </Field>
          <Field label="Active Channel">
            <p className="text-sm">{profile.preferred_channel || 'webchat'}</p>
          </Field>
        </div>
      </Card>
    </div>
  );
}

function TelegramSection({
  profile,
}: {
  profile: { channel_identifier: string; preferred_channel: string };
}) {
  const { isPremium } = useAuth();

  if (isPremium) {
    return <PremiumTelegramSection profile={profile} />;
  }
  return <OssTelegramSection profile={profile} />;
}
