import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import { Divider } from '@heroui/divider';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
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

function TelegramSection({
  profile,
}: {
  profile: { channel_identifier: string; preferred_channel: string };
}) {
  const connected = !!profile.channel_identifier;
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? config?.telegram_allowed_chat_ids ?? '';

  const handleSave = () => {
    if (config && displayedId === config.telegram_allowed_chat_ids) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_allowed_chat_ids: displayedId }, {
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
