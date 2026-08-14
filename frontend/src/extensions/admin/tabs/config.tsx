import { useState, useEffect, useCallback, type ReactNode } from 'react';
import { toast } from '@/lib/toast';
import {
  getAdminChannelConfig,
  updateAdminChannelConfig,
  getAdminLLMConfig,
  updateAdminLLMConfig,
  type AdminChannelConfig,
  type AdminLLMConfig,
} from '../admin-api';
import { LLMModelSelect, LLMProviderSelect } from '../llm-picker';

const inputClass =
  'w-full px-3 py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30';

// ---------------------------------------------------------------------------
// Reusable wrapper: handles its own loading/error so a single failed section
// doesn't blank out the whole tab.
// ---------------------------------------------------------------------------

function SectionShell({
  title,
  loading,
  error,
  onRetry,
  children,
}: {
  title: string;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  children: ReactNode;
}) {
  return (
    <section>
      <h4 className="text-sm font-semibold mb-3">{title}</h4>
      {loading ? (
        <div className="animate-pulse h-24 bg-panel rounded-[--radius-md]" />
      ) : error ? (
        <div className="bg-danger-light border border-danger/30 text-danger text-sm rounded-[--radius-md] px-3 py-2 flex items-center gap-2">
          <span className="flex-1">Couldn't load {title.toLowerCase()}: {error}</span>
          <button
            type="button"
            className="px-2 py-1 text-xs rounded-[--radius-sm] border border-danger/40 hover:bg-danger/10"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      ) : (
        children
      )}
    </section>
  );
}

function StatusPill({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded-[--radius-full] font-medium ${
        ok ? 'bg-success-bg text-success' : 'bg-panel text-muted-foreground'
      }`}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main ConfigTab
// ---------------------------------------------------------------------------

export default function ConfigTab() {
  return (
    <div className="space-y-8">
      <LLMSection />
      <ChannelsSection />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Global LLM default. Per-user overrides live on the user detail page.
// ---------------------------------------------------------------------------

function LLMSection() {
  const [config, setConfig] = useState<AdminLLMConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getAdminLLMConfig()
      .then(setConfig)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <SectionShell title="LLM (global default)" loading={loading} error={error} onRetry={load}>
      {config && <LLMForm config={config} onUpdated={setConfig} />}
    </SectionShell>
  );
}

function LLMForm({
  config,
  onUpdated,
}: {
  config: AdminLLMConfig;
  onUpdated: (next: AdminLLMConfig) => void;
}) {
  const [provider, setProvider] = useState(config.llm_provider);
  const [model, setModel] = useState(config.llm_model);
  const [apiBase, setApiBase] = useState(config.llm_api_base ?? '');
  const [saving, setSaving] = useState(false);

  // When the admin switches provider, the previous model is almost
  // certainly not valid for the new one. Blank it so the model picker
  // forces an explicit choice; the picker will keep an off-list saved
  // value if the admin re-selects the original provider.
  const handleProviderChange = (next: string) => {
    setProvider(next);
    if (next !== provider) setModel('');
  };

  const hasChanges =
    provider !== config.llm_provider ||
    model !== config.llm_model ||
    apiBase !== (config.llm_api_base ?? '');

  const canSave = !!provider && !!model && hasChanges;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    const updates: Record<string, string> = {};
    if (provider !== config.llm_provider) updates.llm_provider = provider;
    if (model !== config.llm_model) updates.llm_model = model;
    if (apiBase !== (config.llm_api_base ?? '')) updates.llm_api_base = apiBase;
    setSaving(true);
    try {
      const updated = await updateAdminLLMConfig(updates);
      onUpdated(updated);
      setProvider(updated.llm_provider);
      setModel(updated.llm_model);
      setApiBase(updated.llm_api_base ?? '');
      toast.success('LLM defaults saved');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="bg-card border border-border rounded-[--radius-md] p-4"
      onSubmit={handleSubmit}
    >
      <p className="text-xs text-muted-foreground mb-3">
        Used for every user that does not have a per-user override set on
        their profile.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label htmlFor="llm-provider" className="text-xs text-muted-foreground block mb-1">
            Provider
          </label>
          <LLMProviderSelect
            id="llm-provider"
            value={provider}
            onChange={handleProviderChange}
          />
        </div>
        <div>
          <label htmlFor="llm-model" className="text-xs text-muted-foreground block mb-1">
            Model
          </label>
          <LLMModelSelect
            id="llm-model"
            provider={provider}
            value={model}
            onChange={setModel}
          />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="llm-api-base" className="text-xs text-muted-foreground block mb-1">
            API base URL <span className="text-muted-foreground">(optional, for self-hosted endpoints)</span>
          </label>
          <input
            id="llm-api-base"
            className={inputClass}
            placeholder="leave blank for the provider default"
            value={apiBase}
            onChange={e => setApiBase(e.target.value)}
          />
        </div>
      </div>
      <div className="mt-3 flex justify-end">
        <button
          type="submit"
          className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
          disabled={!canSave || saving}
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Channels section — shows all channels with their status; inline editor for
// BlueBubbles (the only channel whose settings are mutated via this API).
// ---------------------------------------------------------------------------

function ChannelsSection() {
  const [config, setConfig] = useState<AdminChannelConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getAdminChannelConfig()
      .then(setConfig)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <SectionShell title="Channels" loading={loading} error={error} onRetry={load}>
      {config && (
        <div className="space-y-3">
          {/* Summary row: every channel with its status */}
          <div className="bg-card border border-border rounded-[--radius-md] divide-y divide-border/50">
            <ChannelStatusRow
              name="Telegram"
              subtitle="Requires TELEGRAM_BOT_TOKEN env var"
              ok={!!config.telegram_bot_token_set}
            />
            <ChannelStatusRow
              name="BlueBubbles (iMessage)"
              subtitle="Relay for iMessage via a Mac running the BlueBubbles server"
              ok={!!config.bluebubbles_configured}
            />
            <ChannelStatusRow
              name="Linq (SMS)"
              subtitle="Requires LINQ_API_TOKEN env var"
              ok={!!config.linq_api_token_set}
            />
            <ChannelStatusRow
              name="Twilio (RCS + SMS fallback)"
              subtitle="Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_API_KEY_SID, and TWILIO_API_KEY_SECRET env vars"
              ok={!!config.twilio_configured}
            />
          </div>

          {/* Inline editor for BlueBubbles (fields are DB-backed, not env-only). */}
          <BlueBubblesForm config={config} onUpdated={setConfig} />
        </div>
      )}
    </SectionShell>
  );
}

function ChannelStatusRow({
  name,
  subtitle,
  ok,
}: {
  name: string;
  subtitle: string;
  ok: boolean;
}) {
  return (
    <div className="flex items-center justify-between px-3 py-2.5">
      <div>
        <div className="text-sm font-medium">{name}</div>
        <div className="text-xs text-muted-foreground">{subtitle}</div>
      </div>
      <StatusPill ok={ok}>{ok ? 'Configured' : 'Not configured'}</StatusPill>
    </div>
  );
}

function BlueBubblesForm({
  config,
  onUpdated,
}: {
  config: AdminChannelConfig;
  onUpdated: (next: AdminChannelConfig) => void;
}) {
  const [bbUrl, setBbUrl] = useState(config.bluebubbles_server_url);
  const [bbPassword, setBbPassword] = useState('');
  const [bbImessageAddr, setBbImessageAddr] = useState(
    config.bluebubbles_imessage_address,
  );
  const [saving, setSaving] = useState(false);

  const hasChanges =
    bbUrl !== config.bluebubbles_server_url ||
    bbPassword !== '' ||
    bbImessageAddr !== config.bluebubbles_imessage_address;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!hasChanges) return;
    const updates: Record<string, string> = {};
    if (bbUrl !== config.bluebubbles_server_url) updates.bluebubbles_server_url = bbUrl;
    if (bbPassword) updates.bluebubbles_password = bbPassword;
    if (bbImessageAddr !== config.bluebubbles_imessage_address) {
      updates.bluebubbles_imessage_address = bbImessageAddr;
    }
    setSaving(true);
    try {
      const updated = await updateAdminChannelConfig(updates);
      onUpdated(updated);
      setBbUrl(updated.bluebubbles_server_url);
      setBbImessageAddr(updated.bluebubbles_imessage_address);
      setBbPassword('');
      toast.success('BlueBubbles settings saved');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="bg-card border border-border rounded-[--radius-md] p-4"
      onSubmit={handleSubmit}
    >
      <div className="flex items-center justify-between mb-3">
        <h5 className="text-xs font-semibold text-muted-foreground">
          BlueBubbles settings
        </h5>
        <StatusPill ok={config.bluebubbles_configured}>
          {config.bluebubbles_configured ? 'Configured' : 'Not configured'}
        </StatusPill>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label htmlFor="bb-url" className="text-xs text-muted-foreground block mb-1">
            Server URL
          </label>
          <input
            id="bb-url"
            className={inputClass}
            placeholder="e.g. https://my-mac.ngrok.io"
            value={bbUrl}
            onChange={e => setBbUrl(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="bb-password" className="text-xs text-muted-foreground block mb-1">
            Password{' '}
            {config.bluebubbles_password_set && (
              <span className="text-success">(set)</span>
            )}
          </label>
          <input
            id="bb-password"
            className={inputClass}
            placeholder={
              config.bluebubbles_password_set
                ? 'Leave blank to keep current'
                : 'Server password'
            }
            type="password"
            autoComplete="new-password"
            value={bbPassword}
            onChange={e => setBbPassword(e.target.value)}
          />
        </div>
        <div className="sm:col-span-2">
          <label
            htmlFor="bb-imsg"
            className="text-xs text-muted-foreground block mb-1"
          >
            iMessage address
          </label>
          <input
            id="bb-imsg"
            className={inputClass}
            placeholder="e.g. user@icloud.com or +15551234567"
            value={bbImessageAddr}
            onChange={e => setBbImessageAddr(e.target.value)}
          />
          <p className="text-[11px] text-muted-foreground mt-1">
            Shown to users so they know where to send iMessages.
          </p>
        </div>
      </div>
      <div className="mt-3 flex justify-end">
        <button
          type="submit"
          className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
          disabled={!hasChanges || saving}
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </form>
  );
}

