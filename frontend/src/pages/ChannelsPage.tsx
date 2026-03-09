import { useState, useCallback, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import { toast } from 'sonner';
import api from '@/api';
import type { ChannelConfig } from '@/types';
import type { AppShellContext } from '@/layouts/AppShell';

export default function ChannelsPage() {
  const { profile } = useOutletContext<AppShellContext>();

  if (!profile) return null;

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Channels</h2>
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
  const [config, setConfig] = useState<ChannelConfig | null>(null);
  const [botToken, setBotToken] = useState('');
  const [allowedUsernames, setAllowedUsernames] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getChannelConfig().then((cfg) => {
      setConfig(cfg);
      setAllowedUsernames(cfg.telegram_allowed_usernames);
    }).catch(() => {
      // ignore fetch errors on mount
    });
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const body: Record<string, string> = {};
      if (botToken) body.telegram_bot_token = botToken;
      if (config && allowedUsernames !== config.telegram_allowed_usernames) {
        body.telegram_allowed_usernames = allowedUsernames;
      }
      if (Object.keys(body).length === 0) {
        toast.error('No changes to save');
        setSaving(false);
        return;
      }
      const updated = await api.updateChannelConfig(body);
      setConfig(updated);
      setBotToken('');
      toast.success('Channel config updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [botToken, allowedUsernames, config]);

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Bot Token">
          {config === null ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : (
            <>
              <div className="mb-2">
                {config.telegram_bot_token_set ? (
                  <span className="inline-flex items-center gap-1.5 text-sm">
                    <span className="w-2 h-2 rounded-full bg-green-500 inline-block" />
                    Configured
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-sm">
                    <span className="w-2 h-2 rounded-full bg-red-500 inline-block" />
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
            value={allowedUsernames}
            onChange={(e) => setAllowedUsernames(e.target.value)}
            placeholder='Comma-separated @usernames, or * for all'
          />
          <p className="text-xs text-muted-foreground mt-1">
            Controls which Telegram users can message your bot.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving || config === null}>
            {saving ? 'Saving...' : 'Save Channel Config'}
          </Button>
        </div>
        <hr className="border-border" />
        <Field label="User Connection">
          {connected ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1.5 text-sm">
                <span className="w-2 h-2 rounded-full bg-green-500 inline-block" />
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="text-xs font-medium text-muted-foreground block mb-1">{label}</label>
      {children}
    </div>
  );
}
