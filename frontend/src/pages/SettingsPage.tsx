import { useState, useEffect } from 'react';
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
import { useUpdateProfile } from '@/hooks/queries';
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

  // Refresh profile whenever the settings page is opened.
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
      case 'user': return profile ? (
        <MarkdownSettingsTab
          profile={profile}
          field="user_text"
          label="About You (USER.md)"
          description="Updated over time as your assistant learns about you."
          placeholder="Tell your assistant about yourself: your name, phone, timezone, preferences, what projects you're working on..."
          successMessage="User info updated"
        />
      ) : null;
      case 'soul': return profile ? (
        <MarkdownSettingsTab
          profile={profile}
          field="soul_text"
          label="Personality (SOUL.md)"
          description="Guides your assistant's personality and communication style."
          placeholder="Describe how your assistant should behave, speak, and interact with clients. Include what it should call itself (e.g. 'Your name is Claw')..."
          successMessage="Soul settings updated"
        />
      ) : null;
      case 'heartbeat': return profile ? <HeartbeatTab profile={profile} /> : null;
      default: return null;
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Settings</h2>
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

// --- Generic Markdown Settings Tab ---

function MarkdownSettingsTab({
  profile,
  field,
  label,
  description,
  placeholder,
  successMessage,
}: {
  profile: Record<string, string>;
  field: string;
  label: string;
  description: string;
  placeholder: string;
  successMessage: string;
}) {
  const [text, setText] = useState(profile[field] ?? '');
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    setText(profile[field] ?? '');
  }, [profile, field]);

  const handleSave = () => {
    updateProfile.mutate(
      { [field]: text },
      {
        onSuccess: () => toast.success(successMessage),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between">
        <div>
          <label className="text-xs font-medium text-muted-foreground block mb-1">{label}</label>
          <p className="text-xs text-muted-foreground mt-1">{description}</p>
        </div>
        <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
          Save
        </Button>
      </div>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={28}
        placeholder={placeholder}
      />
    </div>
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
}: {
  profile: { heartbeat_opt_in: boolean; heartbeat_frequency: string };
}) {
  const isPreset = HEARTBEAT_PRESETS.some((p) => p.value === profile.heartbeat_frequency);
  const [form, setForm] = useState({
    heartbeat_opt_in: profile.heartbeat_opt_in,
    heartbeat_frequency: isPreset ? profile.heartbeat_frequency : 'custom',
    custom_frequency: isPreset ? '' : profile.heartbeat_frequency,
  });
  const updateProfile = useUpdateProfile();

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

  const handleSave = () => {
    updateProfile.mutate(
      {
        heartbeat_opt_in: form.heartbeat_opt_in,
        heartbeat_frequency: effectiveFrequency,
      },
      {
        onSuccess: () => toast.success('Heartbeat settings updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

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
            <p className="text-xs text-muted-foreground mt-1">
              Use a number followed by m (minutes), h (hours), or d (days).
            </p>
          </Field>
        )}
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
            Save Heartbeat Settings
          </Button>
        </div>
      </div>
    </Card>
  );
}
