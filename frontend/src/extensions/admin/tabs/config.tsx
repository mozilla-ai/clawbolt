import { useState, useEffect, useCallback, type ReactNode } from 'react';
import { toast } from '@/lib/toast';
import {
  getAdminChannelConfig,
  updateAdminChannelConfig,
  getAdminLLMConfig,
  updateAdminLLMConfig,
  listLLMEndpoints,
  upsertLLMEndpoint,
  deleteLLMEndpoint,
  SECRET_MASK,
  type AdminChannelConfig,
  type AdminLLMConfig,
  type LLMEndpointItem,
  type LLMEndpointUpsert,
} from '../admin-api';
import {
  LLMEndpointSelect,
  LLMModelField,
  LLMProviderSelect,
  ReasoningEffortSelect,
} from '../llm-picker';

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
  // Bumped whenever the editor writes, so the endpoint <select> in the
  // section below refetches. They are siblings, and a new endpoint that
  // cannot be selected until a page reload reads as a failed save.
  const [endpointsVersion, setEndpointsVersion] = useState(0);
  return (
    <div className="space-y-8">
      <LLMEndpointsSection onChanged={() => setEndpointsVersion(v => v + 1)} />
      <LLMSection endpointsVersion={endpointsVersion} />
      <ChannelsSection />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Named endpoints.
//
// A provider name used to answer four questions at once: the wire format,
// whether prompt-cache markers are worth stamping, how to ask for reasoning,
// and which price list applies. Behind a gateway only the first is still
// true. An endpoint answers the other three explicitly.
// ---------------------------------------------------------------------------

const CAPABILITY_HELP: Record<string, string> = {
  cache_control:
    'auto follows the dialect. Use never for a gateway that drops or rejects cache markers.',
  reasoning:
    'auto sends the dialect\'s own shape. Use effort for an OpenAI-style scalar, or none for an endpoint that rejects the parameter even being present.',
  pricing:
    'Mark unpriced when the dialect and model do not identify who billed the tokens, so costs are suppressed rather than invented.',
};

const BLANK_ENDPOINT: LLMEndpointUpsert = {
  name: '',
  dialect: '',
  base_url: '',
  api_key: '',
  cache_control: 'auto',
  reasoning: 'auto',
  pricing: 'auto',
  notes: '',
};

function LLMEndpointsSection({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<LLMEndpointItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The endpoint being edited, or null when the form is closed. A fresh one
  // carries no ``api_key``; an existing one starts with the mask, which the
  // API reads as "leave the stored key alone".
  const [draft, setDraft] = useState<LLMEndpointUpsert | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listLLMEndpoints()
      .then(setItems)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const edit = (item: LLMEndpointItem) =>
    setDraft({
      name: item.name,
      dialect: item.dialect,
      base_url: item.base_url,
      // The key is never sent back to the browser, so the form shows the
      // sentinel and only replaces it when the admin types something.
      api_key: item.api_key_set ? SECRET_MASK : '',
      cache_control: item.cache_control,
      reasoning: item.reasoning,
      pricing: item.pricing,
      notes: item.notes,
    });

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      await upsertLLMEndpoint(draft);
      toast.success(`Endpoint ${draft.name} saved`);
      setDraft(null);
      load();
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (name: string) => {
    try {
      await deleteLLMEndpoint(name);
      toast.success(`Endpoint ${name} deleted`);
      load();
      onChanged();
    } catch (e) {
      // The likely failure is the 409 saying something still selects it,
      // which is acted on by changing that selection first.
      toast.error((e as Error).message);
    }
  };

  const set = (patch: Partial<LLMEndpointUpsert>) =>
    setDraft(prev => (prev ? { ...prev, ...patch } : prev));

  return (
    <SectionShell title="LLM endpoints" loading={loading} error={error} onRetry={load}>
      <div className="space-y-3">
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None configured. Add one to send traffic through a gateway without pretending its
            dialect is the vendor behind it.
          </p>
        ) : (
          <ul className="space-y-2">
            {items.map(item => (
              <li
                key={item.name}
                className="bg-card border border-border rounded-[--radius-md] p-3 flex flex-wrap items-baseline gap-x-3 gap-y-1"
              >
                <span className="text-sm font-medium">{item.name}</span>
                <span className="text-xs text-muted-foreground">{item.dialect}</span>
                <span className="text-xs text-muted-foreground break-all flex-1">
                  {item.base_url || '(provider default)'}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  cache {item.cache_control} | reasoning {item.reasoning} | {item.pricing}
                  {item.api_key_set ? ' | own key' : ''}
                </span>
                <button
                  type="button"
                  className="px-2 py-1 text-xs rounded-[--radius-sm] border border-border hover:bg-panel"
                  onClick={() => edit(item)}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="px-2 py-1 text-xs rounded-[--radius-sm] border border-border hover:bg-danger/10 hover:text-danger"
                  onClick={() => void remove(item.name)}
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}

        {draft ? (
          <div className="bg-card border border-border rounded-[--radius-md] p-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label htmlFor="ep-name" className="text-xs text-muted-foreground block mb-1">
                Name
              </label>
              <input
                id="ep-name"
                className={inputClass}
                placeholder="otari"
                value={draft.name}
                onChange={e => set({ name: e.target.value })}
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                Lowercase letters, digits, dashes. Referenced by settings, so it does not change
                easily once in use.
              </p>
            </div>
            <div>
              <label htmlFor="ep-dialect" className="text-xs text-muted-foreground block mb-1">
                Dialect
              </label>
              <LLMProviderSelect
                id="ep-dialect"
                value={draft.dialect}
                onChange={next => set({ dialect: next })}
                allowEmpty
                emptyLabel="Select the wire format"
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                The API this endpoint speaks, not the vendor behind it.
              </p>
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="ep-base" className="text-xs text-muted-foreground block mb-1">
                Base URL
              </label>
              <input
                id="ep-base"
                className={inputClass}
                placeholder="https://ai.example.com"
                value={draft.base_url ?? ''}
                onChange={e => set({ base_url: e.target.value })}
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="ep-key" className="text-xs text-muted-foreground block mb-1">
                API key
              </label>
              <input
                id="ep-key"
                className={inputClass}
                type="password"
                placeholder="leave blank to use the dialect's own environment variable"
                value={draft.api_key ?? ''}
                onChange={e => set({ api_key: e.target.value })}
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                Set this when the gateway has its own credential, so pointing at a third party does
                not send the key the dialect's vendor issued.
              </p>
            </div>
            {(['cache_control', 'reasoning', 'pricing'] as const).map(field => (
              <div key={field}>
                <label htmlFor={`ep-${field}`} className="text-xs text-muted-foreground block mb-1">
                  {field.replace('_', ' ')}
                </label>
                <select
                  id={`ep-${field}`}
                  className={inputClass}
                  value={draft[field] ?? 'auto'}
                  onChange={e => set({ [field]: e.target.value } as Partial<LLMEndpointUpsert>)}
                >
                  {(field === 'cache_control'
                    ? (['auto', 'always', 'never'] as const)
                    : field === 'reasoning'
                      ? (['auto', 'thinking', 'effort', 'none'] as const)
                      : (['auto', 'unpriced'] as const)
                  ).map(option => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
                <p className="text-[11px] text-muted-foreground mt-1">{CAPABILITY_HELP[field]}</p>
              </div>
            ))}
            <div className="sm:col-span-2 flex justify-end gap-2">
              <button
                type="button"
                className="px-3 py-2 text-sm rounded-[--radius-md] border border-border hover:bg-panel"
                onClick={() => setDraft(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
                disabled={!draft.name || !draft.dialect || saving}
                onClick={() => void save()}
              >
                {saving ? 'Saving...' : 'Save endpoint'}
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="px-3 py-2 text-sm rounded-[--radius-md] border border-border hover:bg-panel"
            onClick={() => setDraft({ ...BLANK_ENDPOINT })}
          >
            Add endpoint
          </button>
        )}
      </div>
    </SectionShell>
  );
}

// ---------------------------------------------------------------------------
// Global LLM default. Per-user overrides live on the user detail page.
// ---------------------------------------------------------------------------

function LLMSection({ endpointsVersion }: { endpointsVersion: number }) {
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
      {config && (
        <LLMForm config={config} onUpdated={setConfig} endpointsVersion={endpointsVersion} />
      )}
    </SectionShell>
  );
}

function LLMForm({
  config,
  onUpdated,
  endpointsVersion,
}: {
  config: AdminLLMConfig;
  onUpdated: (next: AdminLLMConfig) => void;
  endpointsVersion: number;
}) {
  const [endpoint, setEndpoint] = useState(config.llm_endpoint);
  const [provider, setProvider] = useState(config.llm_provider);
  const [model, setModel] = useState(config.llm_model);
  const [apiBase, setApiBase] = useState(config.llm_api_base ?? '');
  const [effort, setEffort] = useState(config.reasoning_effort);
  const [saving, setSaving] = useState(false);

  // When the admin switches provider, the previous model is almost
  // certainly not valid for the new one. Blank it so the model picker
  // forces an explicit choice; the picker will keep an off-list saved
  // value if the admin re-selects the original provider.
  const handleProviderChange = (next: string) => {
    setProvider(next);
    if (next !== provider) setModel('');
  };

  // An endpoint supersedes both the provider and the base URL, so selecting
  // one clears them rather than leaving fields that are displayed and
  // ignored. Reselecting "direct to provider" then leaves an explicit choice
  // to make, which is the honest state.
  const handleEndpointChange = (next: string) => {
    setEndpoint(next);
    if (next) {
      setProvider('');
      setApiBase('');
    }
    setModel('');
  };

  const hasChanges =
    endpoint !== config.llm_endpoint ||
    provider !== config.llm_provider ||
    model !== config.llm_model ||
    apiBase !== (config.llm_api_base ?? '') ||
    effort !== config.reasoning_effort;

  // An endpoint carries its own dialect, so it is a complete destination on
  // its own; without one a provider is still required to have any.
  const canSave = !!(endpoint || provider) && !!model && hasChanges;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    const updates: Record<string, string> = {};
    if (endpoint !== config.llm_endpoint) updates.llm_endpoint = endpoint;
    if (provider !== config.llm_provider) updates.llm_provider = provider;
    if (model !== config.llm_model) updates.llm_model = model;
    if (apiBase !== (config.llm_api_base ?? '')) updates.llm_api_base = apiBase;
    if (effort !== config.reasoning_effort) updates.reasoning_effort = effort;
    setSaving(true);
    try {
      const updated = await updateAdminLLMConfig(updates);
      onUpdated(updated);
      setEndpoint(updated.llm_endpoint);
      setProvider(updated.llm_provider);
      setModel(updated.llm_model);
      setApiBase(updated.llm_api_base ?? '');
      setEffort(updated.reasoning_effort);
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
        <div className="sm:col-span-2">
          <label htmlFor="llm-endpoint" className="text-xs text-muted-foreground block mb-1">
            Endpoint
          </label>
          <LLMEndpointSelect
            id="llm-endpoint"
            value={endpoint}
            onChange={handleEndpointChange}
            refreshKey={endpointsVersion}
          />
          <p className="text-[11px] text-muted-foreground mt-1">
            A named destination carries its own dialect, credential, and capabilities. Selecting one
            supersedes the provider and base URL below.
          </p>
        </div>
        <div>
          <label htmlFor="llm-provider" className="text-xs text-muted-foreground block mb-1">
            Provider
          </label>
          <LLMProviderSelect
            id="llm-provider"
            value={provider}
            onChange={handleProviderChange}
            allowEmpty={!!endpoint}
            emptyLabel="From endpoint"
            disabled={!!endpoint}
          />
        </div>
        <div>
          <label htmlFor="llm-model" className="text-xs text-muted-foreground block mb-1">
            Model
          </label>
          <LLMModelField
            id="llm-model"
            endpoint={endpoint}
            provider={provider}
            value={model}
            onChange={setModel}
          />
        </div>
        <div>
          <label htmlFor="llm-effort" className="text-xs text-muted-foreground block mb-1">
            Reasoning effort
          </label>
          <ReasoningEffortSelect id="llm-effort" value={effort} onChange={setEffort} />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="llm-api-base" className="text-xs text-muted-foreground block mb-1">
            API base URL{' '}
            <span className="text-muted-foreground">(optional, for self-hosted endpoints)</span>
          </label>
          <input
            id="llm-api-base"
            className={inputClass}
            placeholder={
              endpoint ? 'set by the endpoint' : 'leave blank for the provider default'
            }
            value={apiBase}
            disabled={!!endpoint}
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

