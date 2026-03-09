import { useState, useCallback, useEffect } from 'react';
import { Navigate, useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, Tab } from '@heroui/tabs';
import Checkbox from '@/components/ui/checkbox';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import api from '@/api';
import type { AppShellContext } from '@/layouts/AppShell';
import {
  getExtraSettingsTabs,
  renderPremiumSettingsTab,
  showOssSettingsTabs,
} from '@/extensions';

const RETIRED_TABS: Record<string, string> = {
  channels: '/app/channels',
  profile: '/app/settings/user',
  assistant: '/app/settings/soul',
  tools: '/app/tools',
};

export default function SettingsPage() {
  const { tab } = useParams<{ tab: string }>();
  const navigate = useNavigate();
  const { profile, reloadProfile, isPremium, isAdmin } = useOutletContext<AppShellContext>();

  // Fetch the latest profile whenever the settings page is opened.
  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  // Redirect retired tab slugs
  const redirect = tab ? RETIRED_TABS[tab] : undefined;
  if (redirect) {
    return <Navigate to={redirect} replace />;
  }

  const extraTabs = getExtraSettingsTabs(isPremium, isAdmin);
  const activeTab = tab || 'user';

  const handleTabChange = (value: string) => {
    navigate(`/app/settings/${value}`, { replace: true });
  };

  // Build tab list
  const ossTabs = showOssSettingsTabs(isPremium)
    ? [
        { key: 'user', label: 'User' },
        { key: 'soul', label: 'Soul' },
        { key: 'heartbeat', label: 'Heartbeat' },
      ]
    : [];
  const allTabs = [...ossTabs, ...extraTabs.map((t) => ({ key: t.key, label: t.label }))];

  // Premium-only tab
  const premiumContent = renderPremiumSettingsTab(activeTab);

  // Render tab content based on active tab
  const renderContent = () => {
    if (premiumContent) return premiumContent;
    switch (activeTab) {
      case 'user': return profile ? <UserTab profile={profile} onSaved={reloadProfile} /> : null;
      case 'soul': return profile ? <SoulTab profile={profile} onSaved={reloadProfile} /> : null;
      case 'heartbeat': return profile ? <HeartbeatTab profile={profile} onSaved={reloadProfile} /> : null;
      default: return null;
    }
  };

  return (
    <div>
      <h2 className="heading-page mb-6">Settings</h2>
      <Tabs
        selectedKey={activeTab}
        onSelectionChange={(key) => handleTabChange(String(key))}
        variant="underlined"
      >
        {allTabs.map((t) => (
          <Tab key={t.key} title={t.label} />
        ))}
      </Tabs>
      <div className="mt-4">
        {renderContent()}
      </div>
    </div>
  );
}

// --- User Tab (USER.md) ---

function UserTab({
  profile,
  onSaved,
}: {
  profile: { user_text: string };
  onSaved: () => void;
}) {
  const [userText, setUserText] = useState(profile.user_text);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setUserText(profile.user_text);
  }, [profile.user_text]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({ user_text: userText });
      onSaved();
      toast.success('User info updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [userText, onSaved]);

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="About You (USER.md)">
          <Textarea
            value={userText}
            onChange={(e) => setUserText(e.target.value)}
            rows={12}
            placeholder="Tell your assistant about yourself: your name, phone, timezone, preferences, what projects you're working on..."
          />
          <p className="helper-text">
            Everything your assistant knows about you lives here. Updated over time as it learns about you.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// --- Soul Tab (SOUL.md) ---

function SoulTab({
  profile,
  onSaved,
}: {
  profile: { soul_text: string };
  onSaved: () => void;
}) {
  const [soulText, setSoulText] = useState(profile.soul_text);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setSoulText(profile.soul_text);
  }, [profile.soul_text]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({ soul_text: soulText });
      onSaved();
      toast.success('Soul settings updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [soulText, onSaved]);

  return (
    <Card>
      <div className="grid gap-4">
        <Field label="Personality (SOUL.md)">
          <Textarea
            value={soulText}
            onChange={(e) => setSoulText(e.target.value)}
            rows={14}
            placeholder="Describe how your assistant should behave, speak, and interact with clients. Include what it should call itself (e.g. 'Your name is Claw')..."
          />
          <p className="helper-text">
            This guides your assistant's personality, name, and communication style.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
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

  useEffect(() => {
    const preset = HEARTBEAT_PRESETS.some((p) => p.value === profile.heartbeat_frequency);
    setForm({
      heartbeat_opt_in: profile.heartbeat_opt_in,
      heartbeat_frequency: preset ? profile.heartbeat_frequency : 'custom',
      custom_frequency: preset ? '' : profile.heartbeat_frequency,
    });
  }, [profile.heartbeat_opt_in, profile.heartbeat_frequency]);

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
          <Checkbox
            id="heartbeat-opt-in"
            checked={form.heartbeat_opt_in}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_opt_in: e.target.checked }))}
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
            <p className="helper-text">
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

