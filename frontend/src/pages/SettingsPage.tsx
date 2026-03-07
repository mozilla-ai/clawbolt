import { useState, useCallback } from 'react';
import { useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { toast } from 'sonner';
import api from '@/api';
import type { ContractorProfileUpdate } from '@/types';
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
  profile: { name: string; phone: string; trade: string; location: string; hourly_rate: number | null; business_hours: string; timezone: string };
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: profile.name,
    phone: profile.phone,
    trade: profile.trade,
    location: profile.location,
    hourly_rate: profile.hourly_rate?.toString() ?? '',
    business_hours: profile.business_hours,
    timezone: profile.timezone,
  });
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const body: ContractorProfileUpdate = {
        name: form.name,
        phone: form.phone,
        trade: form.trade,
        location: form.location,
        hourly_rate: form.hourly_rate ? parseFloat(form.hourly_rate) : null,
        business_hours: form.business_hours,
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
        <Field label="Trade">
          <Input value={form.trade} onChange={set('trade')} placeholder="e.g. Electrician, Plumber" />
        </Field>
        <Field label="Location">
          <Input value={form.location} onChange={set('location')} placeholder="City, State" />
        </Field>
        <Field label="Hourly Rate ($)">
          <Input value={form.hourly_rate} onChange={set('hourly_rate')} type="number" step="0.01" />
        </Field>
        <Field label="Business Hours">
          <Input value={form.business_hours} onChange={set('business_hours')} placeholder="Mon-Fri 8am-5pm" />
        </Field>
        <Field label="Timezone">
          <Select value={form.timezone} onChange={set('timezone')}>
            <option value="America/New_York">Eastern (ET)</option>
            <option value="America/Chicago">Central (CT)</option>
            <option value="America/Denver">Mountain (MT)</option>
            <option value="America/Los_Angeles">Pacific (PT)</option>
            <option value="America/Anchorage">Alaska (AKT)</option>
            <option value="Pacific/Honolulu">Hawaii (HT)</option>
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
  profile: { assistant_name: string; soul_text: string };
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    assistant_name: profile.assistant_name,
    soul_text: profile.soul_text,
  });
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({
        assistant_name: form.assistant_name,
        soul_text: form.soul_text,
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

function HeartbeatTab({
  profile,
  onSaved,
}: {
  profile: { heartbeat_opt_in: boolean; heartbeat_frequency: string };
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    heartbeat_opt_in: profile.heartbeat_opt_in,
    heartbeat_frequency: profile.heartbeat_frequency,
  });
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({
        heartbeat_opt_in: form.heartbeat_opt_in,
        heartbeat_frequency: form.heartbeat_frequency,
      });
      onSaved();
      toast.success('Heartbeat settings updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [form, onSaved]);

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
            Enable daily heartbeat check-ins
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
            <option value="daily">Daily</option>
            <option value="weekdays">Weekdays only</option>
            <option value="weekly">Weekly</option>
          </Select>
        </Field>
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

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Telegram">
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
