import { useState, useEffect } from 'react';
import { useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, Tab } from '@heroui/tabs';
import Checkbox from '@/components/ui/checkbox';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useModelConfig, useUpdateModelConfig, useUpdateProfile } from '@/hooks/queries';
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

  // Refresh profile whenever the settings page is opened.
  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const extraTabs = getExtraSettingsTabs(isPremium, isAdmin);
  const activeTab = tab || 'model';

  const handleTabChange = (value: string) => {
    navigate(`/app/settings/${value}`, { replace: true });
  };

  // Build tab list
  const ossTabs = showOssSettingsTabs(isPremium)
    ? [
        { key: 'model', label: 'Model' },
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
      case 'model': return <ModelTab />;
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

// --- Model Tab ---

function ModelTab() {
  const { data: config, isLoading } = useModelConfig();
  const updateConfig = useUpdateModelConfig();

  const [form, setForm] = useState({
    llm_provider: '',
    llm_model: '',
    llm_api_base: '',
    vision_model: '',
    heartbeat_model: '',
    heartbeat_provider: '',
    compaction_model: '',
    compaction_provider: '',
  });

  useEffect(() => {
    if (config) {
      setForm({
        llm_provider: config.llm_provider,
        llm_model: config.llm_model,
        llm_api_base: config.llm_api_base ?? '',
        vision_model: config.vision_model,
        heartbeat_model: config.heartbeat_model,
        heartbeat_provider: config.heartbeat_provider,
        compaction_model: config.compaction_model,
        compaction_provider: config.compaction_provider,
      });
    }
  }, [config]);

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>;

  const handleSave = () => {
    updateConfig.mutate(
      {
        llm_provider: form.llm_provider,
        llm_model: form.llm_model,
        llm_api_base: form.llm_api_base || undefined,
        vision_model: form.vision_model,
        heartbeat_model: form.heartbeat_model,
        heartbeat_provider: form.heartbeat_provider,
        compaction_model: form.compaction_model,
        compaction_provider: form.compaction_provider,
      },
      {
        onSuccess: () => toast.success('Model settings saved'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((prev) => ({ ...prev, [key]: e.target.value }));

  return (
    <div className="grid gap-6">
      <Card>
        <h3 className="text-sm font-medium mb-3">Primary Model</h3>
        <div className="grid gap-4">
          <Field label="Provider">
            <Input value={form.llm_provider} onChange={set('llm_provider')} placeholder="e.g. openai, anthropic, openrouter" />
          </Field>
          <Field label="Model">
            <Input value={form.llm_model} onChange={set('llm_model')} placeholder="e.g. gpt-4o, claude-sonnet-4-20250514" />
          </Field>
          <Field label="API Base URL">
            <Input value={form.llm_api_base} onChange={set('llm_api_base')} placeholder="e.g. http://localhost:1234/v1 (optional)" />
            <p className="text-xs text-muted-foreground mt-1">
              Custom API endpoint for local models or proxies. Leave blank for default.
            </p>
          </Field>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-medium mb-1">Task-specific Overrides</h3>
        <p className="text-xs text-muted-foreground mb-3">
          Leave blank to use the primary model for each task.
        </p>
        <div className="grid gap-4">
          <Field label="Vision Model">
            <Input value={form.vision_model} onChange={set('vision_model')} placeholder="Same as primary" />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Heartbeat Provider">
              <Input value={form.heartbeat_provider} onChange={set('heartbeat_provider')} placeholder="Same as primary" />
            </Field>
            <Field label="Heartbeat Model">
              <Input value={form.heartbeat_model} onChange={set('heartbeat_model')} placeholder="Same as primary" />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Compaction Provider">
              <Input value={form.compaction_provider} onChange={set('compaction_provider')} placeholder="Same as primary" />
            </Field>
            <Field label="Compaction Model">
              <Input value={form.compaction_model} onChange={set('compaction_model')} placeholder="Same as primary" />
            </Field>
          </div>
        </div>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateConfig.isPending} isLoading={updateConfig.isPending}>
          Save Model Settings
        </Button>
      </div>
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
          When enabled, your assistant will proactively send you reminders and updates based on your heartbeat items.
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
