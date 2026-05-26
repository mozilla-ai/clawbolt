import { useState, useEffect, useCallback } from 'react';
import { useOutletContext, useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Select from '@/components/ui/select';
import { Tabs, Tab } from '@heroui/tabs';
import Field from '@/components/ui/field';
import api from '@/api';
import { toast } from '@/lib/toast';
import { useModelConfig, useUpdateModelConfig, useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
import DataSharingConsentSection from '@/components/DataSharingConsentSection';
import ChannelsPage from '@/pages/ChannelsPage';
import type { AppShellContext } from '@/layouts/AppShell';
import {
  getExtraSettingsTabs,
  renderPremiumSettingsTab,
  showOssSettingsTabs,
} from '@/extensions';

export default function SettingsPage() {
  const { tab } = useParams<{ tab: string }>();
  const navigate = useNavigate();
  const { reloadProfile, isPremium, isAdmin } = useOutletContext<AppShellContext>();

  // Refresh profile whenever the settings page is opened.
  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const extraTabs = getExtraSettingsTabs(isPremium, isAdmin);

  const handleTabChange = (value: string) => {
    navigate(`/app/settings/${value}`, { replace: true });
  };

  // Build tab list
  const visibleOssKeys = showOssSettingsTabs(isPremium, isAdmin);
  const ossTabs = [
    { key: 'model', label: 'Model' },
    { key: 'telegram', label: 'Telegram' },
    { key: 'channels', label: 'Channels' },
    { key: 'privacy', label: 'Privacy' },
  ].filter((t) => visibleOssKeys.includes(t.key));
  const allTabs = [...ossTabs, ...extraTabs.map((t) => ({ key: t.key, label: t.label }))];
  const activeTab = (tab && allTabs.some((t) => t.key === tab)) ? tab : allTabs[0]?.key || 'model';

  // Premium-only tab
  const premiumContent = renderPremiumSettingsTab(activeTab, isAdmin);

  // Render tab content based on active tab
  const renderContent = () => {
    if (premiumContent) return premiumContent;
    switch (activeTab) {
      case 'model': return <ModelTab />;
      case 'telegram': return <TelegramTab />;
      case 'channels': return <ChannelsPage />;
      case 'privacy': return <PrivacyTab />;
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

// --- Privacy Tab ---

function PrivacyTab() {
  return (
    <Card>
      <DataSharingConsentSection />
    </Card>
  );
}
