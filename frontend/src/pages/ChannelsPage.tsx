import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Divider from '@/components/ui/divider';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
import type { AppShellContext } from '@/layouts/AppShell';

export default function ChannelsPage() {
  const { profile } = useOutletContext<AppShellContext>();

  if (!profile) return null;

  return (
    <div>
      <h2 className="heading-page mb-6">Channels</h2>
      <ChannelsContent profile={profile} />
    </div>
  );
}

function ChannelsContent({
  profile,
}: {
  profile: { channel_identifier: string; preferred_channel: string };
}) {
  const connected = !!profile.channel_identifier;
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [botToken, setBotToken] = useState('');
  const [allowedUsernames, setAllowedUsernames] = useState<string | null>(null);

  // Use local state if edited, otherwise fall back to server data
  const displayedUsernames = allowedUsernames ?? config?.telegram_allowed_usernames ?? '';

  const handleSave = () => {
    const body: Record<string, string> = {};
    if (botToken) body.telegram_bot_token = botToken;
    if (config && displayedUsernames !== config.telegram_allowed_usernames) {
      body.telegram_allowed_usernames = displayedUsernames;
    }
    if (Object.keys(body).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(body, {
      onSuccess: () => {
        setBotToken('');
        setAllowedUsernames(null);
        toast.success('Channel config updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Bot Token">
          {config === undefined ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : (
            <>
              <div className="mb-2">
                {config.telegram_bot_token_set ? (
                  <span className="inline-flex items-center gap-1.5 text-sm">
                    <span className="status-dot bg-success" />
                    Configured
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-sm">
                    <span className="status-dot bg-danger" />
                    Not configured
                  </span>
                )}
              </div>
              <Input
                type="password"
                value={botToken}
                onChange={(e) => setBotToken(e.target.value)}
                placeholder={config.telegram_bot_token_set ? 'Enter new token to replace' : 'Paste bot token from @BotFather'}
              />
            </>
          )}
        </Field>
        <Field label="Allowed Usernames">
          <Input
            value={displayedUsernames}
            onChange={(e) => setAllowedUsernames(e.target.value)}
            placeholder='Comma-separated @usernames, or * for all'
          />
          <p className="helper-text">
            Controls which Telegram users can message your bot.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
            Save Channel Config
          </Button>
        </div>
        <Divider />
        <Field label="User Connection">
          {connected ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1.5 text-sm">
                <span className="status-dot bg-success" />
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
  );
}
