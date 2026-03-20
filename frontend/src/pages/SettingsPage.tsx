import { useState, useEffect, useCallback } from 'react';
import { useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, Tab } from '@heroui/tabs';
import Checkbox from '@/components/ui/checkbox';
import Field from '@/components/ui/field';
import api from '@/api';
import { toast } from '@/lib/toast';
import { useModelConfig, useUpdateModelConfig, useStorageConfig, useUpdateStorageConfig, useUpdateProfile, useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
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
  const visibleOssKeys = showOssSettingsTabs(isPremium, isAdmin);
  const ossTabs = [
    { key: 'model', label: 'Model' },
    { key: 'storage', label: 'Storage' },
    { key: 'heartbeat', label: 'Heartbeat' },
    { key: 'telegram', label: 'Telegram' },
  ].filter((t) => visibleOssKeys.includes(t.key));
  const allTabs = [...ossTabs, ...extraTabs.map((t) => ({ key: t.key, label: t.label }))];

  // Premium-only tab
  const premiumContent = renderPremiumSettingsTab(activeTab);

  // Render tab content based on active tab
  const renderContent = () => {
    if (premiumContent) return premiumContent;
    switch (activeTab) {
      case 'model': return <ModelTab />;
      case 'storage': return <StorageTab />;
      case 'heartbeat': return profile ? <HeartbeatTab profile={profile} /> : null;
      case 'telegram': return <TelegramTab />;
      default: return null;
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Settings</h2>
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

/** Hook to fetch the list of providers once and cache it. */
function useProviders() {
  const [providers, setProviders] = useState<{ name: string; local: boolean }[]>([]);
  useEffect(() => {
    api.listProviders().then(setProviders).catch(() => {});
  }, []);
  return providers;
}

/**
 * Hook that fetches models whenever the provider (or apiBase for local providers) changes.
 * Returns { models, loading, error }.
 */
function useProviderModels(provider: string, isLocal: boolean) {
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchModels = useCallback((prov: string, base: string) => {
    if (!prov) return;
    setLoading(true);
    setError('');
    api.listProviderModels(prov, base || undefined)
      .then((list) => { setModels(list); })
      .catch((err) => { setError((err as Error).message); setModels([]); })
      .finally(() => setLoading(false));
  }, []);

  // Auto-fetch for cloud providers when provider changes
  useEffect(() => {
    if (!provider || isLocal) { setModels([]); return; }
    fetchModels(provider, '');
  }, [provider, isLocal, fetchModels]);

  return { models, loading, error, fetchModels };
}

/** A provider + model picker row. */
function ProviderModelPicker({
  providers,
  providerValue,
  modelValue,
  apiBaseValue,
  onProviderChange,
  onModelChange,
  onApiBaseChange,
  showApiBase,
  placeholderModel,
}: {
  providers: { name: string; local: boolean }[];
  providerValue: string;
  modelValue: string;
  apiBaseValue?: string;
  onProviderChange: (v: string) => void;
  onModelChange: (v: string) => void;
  onApiBaseChange?: (v: string) => void;
  showApiBase?: boolean;
  placeholderModel?: string;
}) {
  const isLocal = providers.find((p) => p.name === providerValue)?.local ?? false;
  const { models, loading, error, fetchModels } = useProviderModels(providerValue, isLocal);

  return (
    <div className="grid gap-4">
      <Field label="Provider">
        <Select
          value={providerValue}
          onChange={(e) => {
            onProviderChange(e.target.value);
            onModelChange('');
          }}
        >
          <option value="">Select provider...</option>
          {providers.map((p) => (
            <option key={p.name} value={p.name}>{p.name}</option>
          ))}
        </Select>
      </Field>

      {providerValue && isLocal && showApiBase && (
        <Field label="API Base URL">
          <div className="flex gap-2">
            <Input
              value={apiBaseValue ?? ''}
              onChange={(e) => onApiBaseChange?.(e.target.value)}
              placeholder="e.g. http://localhost:1234/v1"
              className="flex-1"
            />
            <Button
              variant="secondary"
              onClick={() => fetchModels(providerValue, apiBaseValue ?? '')}
              disabled={!apiBaseValue || loading}
            >
              {loading ? 'Fetching...' : 'Fetch Models'}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            Custom API endpoint for local models or proxies.
          </p>
        </Field>
      )}

      <Field label="Model">
        {loading ? (
          <Select disabled><option value="">Loading models...</option></Select>
        ) : models.length > 0 ? (
          <Select
            value={modelValue}
            onChange={(e) => onModelChange(e.target.value)}
          >
            <option value="">{placeholderModel ?? 'Select model...'}</option>
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </Select>
        ) : (
          <Input
            value={modelValue}
            onChange={(e) => onModelChange(e.target.value)}
            placeholder={placeholderModel ?? 'e.g. gpt-4o, claude-sonnet-4-20250514'}
          />
        )}
        {error && <p className="text-xs text-danger mt-1">{error}</p>}
      </Field>
    </div>
  );
}

const REASONING_EFFORT_OPTIONS = [
  { value: 'auto', label: 'Auto (provider default)' },
  { value: 'none', label: 'None' },
  { value: 'minimal', label: 'Minimal' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra High' },
] as const;

function ModelTab() {
  const { data: config, isLoading } = useModelConfig();
  const updateConfig = useUpdateModelConfig();
  const providers = useProviders();

  const [form, setForm] = useState({
    llm_provider: '',
    llm_model: '',
    llm_api_base: '',
    vision_model: '',
    vision_provider: '',
    heartbeat_model: '',
    heartbeat_provider: '',
    compaction_model: '',
    compaction_provider: '',
    reasoning_effort: 'auto',
  });

  useEffect(() => {
    if (config) {
      setForm({
        llm_provider: config.llm_provider,
        llm_model: config.llm_model,
        llm_api_base: config.llm_api_base ?? '',
        vision_model: config.vision_model,
        vision_provider: config.vision_provider,
        heartbeat_model: config.heartbeat_model,
        heartbeat_provider: config.heartbeat_provider,
        compaction_model: config.compaction_model,
        compaction_provider: config.compaction_provider,
        reasoning_effort: config.reasoning_effort,
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
        vision_provider: form.vision_provider,
        heartbeat_model: form.heartbeat_model,
        heartbeat_provider: form.heartbeat_provider,
        compaction_model: form.compaction_model,
        compaction_provider: form.compaction_provider,
        reasoning_effort: form.reasoning_effort,
      },
      {
        onSuccess: () => toast.success('Model settings saved'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const set = (key: string, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="grid gap-6">
      <Card>
        <h3 className="text-sm font-medium mb-3">Primary Model</h3>
        <ProviderModelPicker
          providers={providers}
          providerValue={form.llm_provider}
          modelValue={form.llm_model}
          apiBaseValue={form.llm_api_base}
          onProviderChange={(v) => set('llm_provider', v)}
          onModelChange={(v) => set('llm_model', v)}
          onApiBaseChange={(v) => set('llm_api_base', v)}
          showApiBase
        />
        <Field label="Reasoning Effort">
          <Select
            value={form.reasoning_effort}
            onChange={(e) => set('reasoning_effort', e.target.value)}
          >
            {REASONING_EFFORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </Select>
          <p className="text-xs text-muted-foreground mt-1">
            Controls how much reasoning the model uses. Higher values produce more thorough responses but use more tokens.
          </p>
        </Field>
      </Card>

      <Card>
        <h3 className="text-sm font-medium mb-1">Task-specific Overrides</h3>
        <p className="text-xs text-muted-foreground mb-3">
          Leave blank to use the primary model for each task.
        </p>
        <div className="grid gap-4">
          <div>
            <p className="text-xs font-medium mb-3">Vision</p>
            <ProviderModelPicker
              providers={providers}
              providerValue={form.vision_provider}
              modelValue={form.vision_model}
              onProviderChange={(v) => set('vision_provider', v)}
              onModelChange={(v) => set('vision_model', v)}
              placeholderModel="Same as primary"
            />
          </div>
          <div className="border-t pt-4">
            <p className="text-xs font-medium mb-3">Heartbeat</p>
            <ProviderModelPicker
              providers={providers}
              providerValue={form.heartbeat_provider}
              modelValue={form.heartbeat_model}
              onProviderChange={(v) => set('heartbeat_provider', v)}
              onModelChange={(v) => set('heartbeat_model', v)}
              placeholderModel="Same as primary"
            />
          </div>
          <div className="border-t pt-4">
            <p className="text-xs font-medium mb-3">Compaction</p>
            <ProviderModelPicker
              providers={providers}
              providerValue={form.compaction_provider}
              modelValue={form.compaction_model}
              onProviderChange={(v) => set('compaction_provider', v)}
              onModelChange={(v) => set('compaction_model', v)}
              placeholderModel="Same as primary"
            />
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

// --- Storage Tab ---

const STORAGE_PROVIDER_OPTIONS = [
  { value: 'local', label: 'Local filesystem' },
  { value: 'dropbox', label: 'Dropbox' },
  { value: 'google_drive', label: 'Google Drive' },
] as const;

function StorageTab() {
  const { data: config, isLoading } = useStorageConfig();
  const updateConfig = useUpdateStorageConfig();

  const [form, setForm] = useState({
    storage_provider: 'local',
    file_storage_base_dir: '',
    dropbox_access_token: '',
    google_drive_credentials_json: '',
  });

  useEffect(() => {
    if (config) {
      setForm({
        storage_provider: config.storage_provider,
        file_storage_base_dir: config.file_storage_base_dir,
        dropbox_access_token: '',
        google_drive_credentials_json: '',
      });
    }
  }, [config]);

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>;

  const handleSave = () => {
    const body: Record<string, string> = {
      storage_provider: form.storage_provider,
      file_storage_base_dir: form.file_storage_base_dir,
    };
    if (form.dropbox_access_token) {
      body.dropbox_access_token = form.dropbox_access_token;
    }
    if (form.google_drive_credentials_json) {
      body.google_drive_credentials_json = form.google_drive_credentials_json;
    }
    updateConfig.mutate(body, {
      onSuccess: () => {
        toast.success('Storage settings saved');
        setForm((prev) => ({
          ...prev,
          dropbox_access_token: '',
          google_drive_credentials_json: '',
        }));
      },
      onError: (e) => toast.error(e.message),
    });
  };

  const set = (key: string, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="grid gap-6">
      <Card>
        <div className="grid gap-4">
          <Field label="Storage Provider">
            <Select
              value={form.storage_provider}
              onChange={(e) => set('storage_provider', e.target.value)}
            >
              {STORAGE_PROVIDER_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </Select>
          </Field>

          {form.storage_provider === 'local' && (
            <Field label="Storage Directory">
              <Input
                value={form.file_storage_base_dir}
                onChange={(e) => set('file_storage_base_dir', e.target.value)}
                placeholder="data/storage"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Local directory for file storage. Relative to the app root.
              </p>
            </Field>
          )}

          {form.storage_provider === 'dropbox' && (
            <>
              <p className="text-sm">
                Status:{' '}
                <span className={config?.dropbox_access_token_set ? 'text-success' : 'text-warning'}>
                  {config?.dropbox_access_token_set ? 'Configured' : 'Not configured'}
                </span>
              </p>
              <Field label="Access Token">
                <Input
                  type="password"
                  value={form.dropbox_access_token}
                  onChange={(e) => set('dropbox_access_token', e.target.value)}
                  placeholder={config?.dropbox_access_token_set ? 'Leave blank to keep current token' : 'Enter Dropbox access token'}
                />
              </Field>
            </>
          )}

          {form.storage_provider === 'google_drive' && (
            <>
              <p className="text-sm">
                Status:{' '}
                <span className={config?.google_drive_credentials_json_set ? 'text-success' : 'text-warning'}>
                  {config?.google_drive_credentials_json_set ? 'Configured' : 'Not configured'}
                </span>
              </p>
              <Field label="Credentials JSON">
                <textarea
                  className="w-full rounded-md border border-default bg-card px-3 py-2 text-sm font-mono min-h-[120px]"
                  value={form.google_drive_credentials_json}
                  onChange={(e) => set('google_drive_credentials_json', e.target.value)}
                  placeholder={config?.google_drive_credentials_json_set ? 'Leave blank to keep current credentials' : 'Paste Google Drive service account JSON'}
                />
              </Field>
            </>
          )}
        </div>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateConfig.isPending} isLoading={updateConfig.isPending}>
          Save Storage Settings
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

// --- Telegram Tab ---

function TelegramTab() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [botToken, setBotToken] = useState('');

  const handleSave = () => {
    if (!botToken) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_bot_token: botToken }, {
      onSuccess: () => {
        setBotToken('');
        toast.success('Telegram settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-6">
      <Card>
        <h3 className="text-sm font-medium mb-3">Bot Configuration</h3>
        <div className="grid gap-4">
          <Field label="Bot Token">
            {config === undefined ? (
              <p className="text-sm text-muted-foreground">Loading...</p>
            ) : (
              <>
                <div className="mb-2">
                  {config.telegram_bot_token_set ? (
                    <span className="inline-flex items-center gap-1.5 text-sm">
                      <span className="size-2 rounded-full inline-block shrink-0 bg-success" />
                      Configured
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 text-sm">
                      <span className="size-2 rounded-full inline-block shrink-0 bg-danger" />
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
        </div>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
          Save Telegram Settings
        </Button>
      </div>
    </div>
  );
}
