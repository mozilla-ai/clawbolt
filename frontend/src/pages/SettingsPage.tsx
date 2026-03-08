import { useState, useCallback, useEffect } from 'react';
import { useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { toast } from 'sonner';
import api from '@/api';
import type { ChannelConfig, ContractorProfileUpdate } from '@/types';
import type { AppShellContext } from '@/layouts/AppShell';
import {
  getExtraSettingsTabs,
  renderPremiumSettingsTab,
  showOssSettingsTabs,
} from '@/extensions';

export default function SettingsPage() {
  const { tab } = useParams<{ tab: string }>();
  const navigate = useNavigate();
  const { profile, reloadProfile, isPremium, isAdmin } = useOutletContext<AppShellContext>();

  const extraTabs = getExtraSettingsTabs(isPremium, isAdmin);
  const activeTab = tab || 'profile';

  const handleTabChange = (value: string) => {
    navigate(`/app/settings/${value}`, { replace: true });
  };

  // Premium-only tab
  const premiumContent = renderPremiumSettingsTab(activeTab);
  if (premiumContent) {
    return (
      <div>
        <h2 className="text-xl font-semibold mb-6">Settings</h2>
        <Tabs value={activeTab} onValueChange={handleTabChange}>
          <TabsList>
            {showOssSettingsTabs(isPremium) && (
              <>
                <TabsTrigger value="profile">Profile</TabsTrigger>
                <TabsTrigger value="assistant">Assistant</TabsTrigger>
                <TabsTrigger value="heartbeat">Heartbeat</TabsTrigger>
                <TabsTrigger value="channels">Channels</TabsTrigger>
              </>
            )}
            {extraTabs.map((t) => (
              <TabsTrigger key={t.key} value={t.key}>{t.label}</TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value={activeTab}>
            {premiumContent}
          </TabsContent>
        </Tabs>
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Settings</h2>
      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="assistant">Assistant</TabsTrigger>
          <TabsTrigger value="heartbeat">Heartbeat</TabsTrigger>
          <TabsTrigger value="channels">Channels</TabsTrigger>
          {extraTabs.map((t) => (
            <TabsTrigger key={t.key} value={t.key}>{t.label}</TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="profile">
          {profile && <ProfileTab profile={profile} onSaved={reloadProfile} />}
        </TabsContent>

        <TabsContent value="assistant">
          {profile && <AssistantTab profile={profile} onSaved={reloadProfile} />}
        </TabsContent>

        <TabsContent value="heartbeat">
          {profile && <HeartbeatTab profile={profile} onSaved={reloadProfile} />}
        </TabsContent>

        <TabsContent value="channels">
          {profile && <ChannelsTab profile={profile} />}
        </TabsContent>
      </Tabs>
    </div>
  );
}

// --- Profile Tab ---

function ProfileTab({
  profile,
  onSaved,
}: {
  profile: { name: string; phone: string; timezone: string };
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: profile.name,
    phone: profile.phone,
    timezone: profile.timezone,
  });
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const body: ContractorProfileUpdate = {
        name: form.name,
        phone: form.phone,
        timezone: form.timezone,
      };
      await api.updateProfile(body);
      onSaved();
      toast.success('Profile updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [form, onSaved]);

  const set = (field: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((prev) => ({ ...prev, [field]: e.target.value }));

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Name">
          <Input value={form.name} onChange={set('name')} />
        </Field>
        <Field label="Phone">
          <Input value={form.phone} onChange={set('phone')} type="tel" />
        </Field>
        <Field label="Timezone">
          <Select value={form.timezone} onChange={set('timezone')}>
            <optgroup label="Americas">
              <option value="America/New_York">Eastern (ET)</option>
              <option value="America/Chicago">Central (CT)</option>
              <option value="America/Denver">Mountain (MT)</option>
              <option value="America/Los_Angeles">Pacific (PT)</option>
              <option value="America/Anchorage">Alaska (AKT)</option>
              <option value="Pacific/Honolulu">Hawaii (HT)</option>
              <option value="America/Phoenix">Arizona (no DST)</option>
              <option value="America/Toronto">Toronto (ET)</option>
              <option value="America/Vancouver">Vancouver (PT)</option>
              <option value="America/Mexico_City">Mexico City (CST)</option>
              <option value="America/Sao_Paulo">Sao Paulo (BRT)</option>
              <option value="America/Argentina/Buenos_Aires">Buenos Aires (ART)</option>
              <option value="America/Bogota">Bogota (COT)</option>
            </optgroup>
            <optgroup label="Europe">
              <option value="Europe/London">London (GMT/BST)</option>
              <option value="Europe/Paris">Paris (CET)</option>
              <option value="Europe/Berlin">Berlin (CET)</option>
              <option value="Europe/Madrid">Madrid (CET)</option>
              <option value="Europe/Rome">Rome (CET)</option>
              <option value="Europe/Amsterdam">Amsterdam (CET)</option>
              <option value="Europe/Zurich">Zurich (CET)</option>
              <option value="Europe/Stockholm">Stockholm (CET)</option>
              <option value="Europe/Athens">Athens (EET)</option>
              <option value="Europe/Istanbul">Istanbul (TRT)</option>
              <option value="Europe/Moscow">Moscow (MSK)</option>
            </optgroup>
            <optgroup label="Asia / Pacific">
              <option value="Asia/Dubai">Dubai (GST)</option>
              <option value="Asia/Kolkata">India (IST)</option>
              <option value="Asia/Singapore">Singapore (SGT)</option>
              <option value="Asia/Hong_Kong">Hong Kong (HKT)</option>
              <option value="Asia/Shanghai">Shanghai (CST)</option>
              <option value="Asia/Tokyo">Tokyo (JST)</option>
              <option value="Asia/Seoul">Seoul (KST)</option>
              <option value="Asia/Jakarta">Jakarta (WIB)</option>
              <option value="Asia/Manila">Manila (PHT)</option>
            </optgroup>
            <optgroup label="Oceania">
              <option value="Australia/Sydney">Sydney (AEST)</option>
              <option value="Australia/Melbourne">Melbourne (AEST)</option>
              <option value="Australia/Perth">Perth (AWST)</option>
              <option value="Australia/Brisbane">Brisbane (AEST, no DST)</option>
              <option value="Pacific/Auckland">Auckland (NZST)</option>
            </optgroup>
            <optgroup label="Africa / Middle East">
              <option value="Africa/Cairo">Cairo (EET)</option>
              <option value="Africa/Lagos">Lagos (WAT)</option>
              <option value="Africa/Johannesburg">Johannesburg (SAST)</option>
              <option value="Africa/Nairobi">Nairobi (EAT)</option>
              <option value="Asia/Riyadh">Riyadh (AST)</option>
              <option value="Asia/Tehran">Tehran (IRST)</option>
            </optgroup>
          </Select>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save Profile'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// --- Assistant Tab ---

function AssistantTab({
  profile,
  onSaved,
}: {
  profile: { assistant_name: string; soul_text: string; user_text: string };
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    assistant_name: profile.assistant_name,
    soul_text: profile.soul_text,
    user_text: profile.user_text,
  });
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({
        assistant_name: form.assistant_name,
        soul_text: form.soul_text,
        user_text: form.user_text,
      });
      onSaved();
      toast.success('Assistant settings updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [form, onSaved]);

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Assistant Name">
          <Input
            value={form.assistant_name}
            onChange={(e) => setForm((prev) => ({ ...prev, assistant_name: e.target.value }))}
            placeholder="e.g. Claw"
          />
        </Field>
        <Field label="Personality / SOUL">
          <Textarea
            value={form.soul_text}
            onChange={(e) => setForm((prev) => ({ ...prev, soul_text: e.target.value }))}
            rows={8}
            placeholder="Describe how your assistant should behave, speak, and interact with clients..."
          />
          <p className="text-xs text-muted-foreground mt-1">
            This guides your assistant's personality and communication style.
          </p>
        </Field>
        <Field label="About You / USER">
          <Textarea
            value={form.user_text}
            onChange={(e) => setForm((prev) => ({ ...prev, user_text: e.target.value }))}
            rows={6}
            placeholder="Tell your assistant about yourself: your name, preferences, what projects you're working on..."
          />
          <p className="text-xs text-muted-foreground mt-1">
            Helps your assistant personalize responses. Updated over time as it learns about you.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save Assistant Settings'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// --- Heartbeat Tab ---

const HEARTBEAT_PRESETS = [
  { value: '15m', label: 'Every 15 minutes' },
  { value: '30m', label: 'Every 30 minutes' },
  { value: '1h', label: 'Every hour' },
  { value: '2h', label: 'Every 2 hours' },
  { value: '4h', label: 'Every 4 hours' },
  { value: '8h', label: 'Every 8 hours' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekdays', label: 'Weekdays only' },
  { value: 'weekly', label: 'Weekly' },
] as const;

function HeartbeatTab({
  profile,
  onSaved,
}: {
  profile: { heartbeat_opt_in: boolean; heartbeat_frequency: string };
  onSaved: () => void;
}) {
  const isPreset = HEARTBEAT_PRESETS.some((p) => p.value === profile.heartbeat_frequency);
  const [form, setForm] = useState({
    heartbeat_opt_in: profile.heartbeat_opt_in,
    heartbeat_frequency: isPreset ? profile.heartbeat_frequency : 'custom',
    custom_frequency: isPreset ? '' : profile.heartbeat_frequency,
  });
  const [saving, setSaving] = useState(false);

  const effectiveFrequency = form.heartbeat_frequency === 'custom'
    ? form.custom_frequency
    : form.heartbeat_frequency;

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({
        heartbeat_opt_in: form.heartbeat_opt_in,
        heartbeat_frequency: effectiveFrequency,
      });
      onSaved();
      toast.success('Heartbeat settings updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [form, effectiveFrequency, onSaved]);

  return (
    <Card>
      <div className="grid gap-4">
        <div className="flex items-center gap-3">
          <input
            type="checkbox"
            id="heartbeat-opt-in"
            checked={form.heartbeat_opt_in}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_opt_in: e.target.checked }))}
            className="w-4 h-4 rounded border-border text-primary focus:ring-primary"
          />
          <label htmlFor="heartbeat-opt-in" className="text-sm">
            Enable heartbeat check-ins
          </label>
        </div>
        <p className="text-xs text-muted-foreground">
          When enabled, your assistant will proactively send you reminders and updates based on your checklist.
        </p>
        <Field label="Frequency">
          <Select
            value={form.heartbeat_frequency}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_frequency: e.target.value }))}
            disabled={!form.heartbeat_opt_in}
          >
            {HEARTBEAT_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
            <option value="custom">Custom interval</option>
          </Select>
        </Field>
        {form.heartbeat_frequency === 'custom' && (
          <Field label="Custom Interval">
            <Input
              value={form.custom_frequency}
              onChange={(e) => setForm((prev) => ({ ...prev, custom_frequency: e.target.value }))}
              disabled={!form.heartbeat_opt_in}
              placeholder="e.g. 45m, 3h, 2d"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Use a number followed by m (minutes), h (hours), or d (days).
            </p>
          </Field>
        )}
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save Heartbeat Settings'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// --- Channels Tab ---

function ChannelsTab({
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

// --- Shared field wrapper ---

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="text-xs font-medium text-muted-foreground block mb-1">{label}</label>
      {children}
    </div>
  );
}
