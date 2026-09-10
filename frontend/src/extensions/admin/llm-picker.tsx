import { useState, useEffect, useCallback } from 'react';
import {
  invalidateProviderModels,
  listLLMEndpoints,
  listProviders,
  listProviderModels,
  type LLMEndpointItem,
  type ProviderInfo,
  type ProviderModelsResult,
} from './admin-api';

const inputClass =
  'w-full px-3 py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30';

const selectClass = inputClass;

// ---------------------------------------------------------------------------
// Provider <select>: populated from any-llm's known-provider enumeration.
// Always a real dropdown, never freetext. Optional empty-state lets the
// per-user override form represent "use the global default".
// ---------------------------------------------------------------------------

interface LLMProviderSelectProps {
  id?: string;
  value: string;
  onChange: (next: string) => void;
  /** When true, prepend an empty option with ``emptyLabel``. */
  allowEmpty?: boolean;
  emptyLabel?: string;
  disabled?: boolean;
}

export function LLMProviderSelect({
  id,
  value,
  onChange,
  allowEmpty,
  emptyLabel = '(use global)',
  disabled,
}: LLMProviderSelectProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    listProviders()
      .then(p => setProviders(p))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <select
        id={id}
        className={selectClass}
        disabled
        value=""
        onChange={() => {}}
        aria-label="Provider (loading)"
      >
        <option value="">Loading providers...</option>
      </select>
    );
  }

  if (error) {
    // Hard fail on provider enumeration is unusual; fall back to a text
    // input so the admin is not stuck.
    return (
      <div>
        <input
          id={id}
          className={inputClass}
          placeholder="provider id"
          value={value}
          disabled={disabled}
          onChange={e => onChange(e.target.value)}
        />
        <p className="text-[11px] text-danger mt-1">
          Failed to load provider list: {error}
        </p>
      </div>
    );
  }

  // Saved value not in the enum: surface it as a "(not recognised)" option so
  // the form does not silently blank it on render.
  const knownNames = new Set(providers.map(p => p.name));
  const showSavedOutsideList = value && !knownNames.has(value);

  return (
    <select
      id={id}
      className={selectClass}
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
    >
      {allowEmpty && <option value="">{emptyLabel}</option>}
      {showSavedOutsideList && (
        <option value={value}>{value} (saved, not in list)</option>
      )}
      {providers.map(p => (
        <option key={p.name} value={p.name}>
          {p.name}
          {p.local ? ' (local)' : ''}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Model select with structured-error fallback.
//
// The admin endpoint returns ``{ models, supports_listing, error }``. Render
// strategy:
//   - models non-empty, no error: <select>
//   - supports_listing == false: <input> + note ("provider does not list models")
//   - error set:                  <input> + inline error + Retry button
//   - provider unset:             disabled <select> with placeholder
// ---------------------------------------------------------------------------

interface LLMModelSelectProps {
  id?: string;
  provider: string;
  value: string;
  onChange: (next: string) => void;
  allowEmpty?: boolean;
  emptyLabel?: string;
}

export function LLMModelSelect({
  id,
  provider,
  value,
  onChange,
  allowEmpty,
  emptyLabel = '(use global)',
}: LLMModelSelectProps) {
  const [result, setResult] = useState<ProviderModelsResult | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    if (!provider) {
      setResult(null);
      return;
    }
    setLoading(true);
    listProviderModels(provider)
      .then(setResult)
      .catch((e: Error) =>
        setResult({
          provider,
          models: [],
          supports_listing: true,
          error: e.message,
        }),
      )
      .finally(() => setLoading(false));
  }, [provider]);

  useEffect(() => {
    load();
  }, [load]);

  const retry = () => {
    invalidateProviderModels(provider);
    load();
  };

  if (!provider) {
    return (
      <select id={id} className={selectClass} disabled value="" onChange={() => {}}>
        <option value="">Pick a provider first</option>
      </select>
    );
  }

  if (loading || !result) {
    return (
      <select id={id} className={selectClass} disabled value="" onChange={() => {}}>
        <option value="">Loading models...</option>
      </select>
    );
  }

  // Provider explicitly does not enumerate models: text input with note.
  if (!result.supports_listing) {
    return (
      <div>
        <input
          id={id}
          className={inputClass}
          placeholder="model id"
          value={value}
          onChange={e => onChange(e.target.value)}
        />
        <p className="text-[11px] text-muted-foreground mt-1">
          {result.error ||
            'This provider does not enumerate models. Type the model id directly.'}
        </p>
      </div>
    );
  }

  // Listing failed (missing key, transient network, etc.). Same shape as the
  // unsupported case but with a Retry button.
  if (result.error || result.models.length === 0) {
    return (
      <div>
        <input
          id={id}
          className={inputClass}
          placeholder="model id"
          value={value}
          onChange={e => onChange(e.target.value)}
        />
        <p className="text-[11px] text-danger mt-1 flex items-center gap-2">
          <span>{result.error || 'No models returned.'}</span>
          <button
            type="button"
            onClick={retry}
            className="text-primary hover:underline"
          >
            Retry
          </button>
        </p>
      </div>
    );
  }

  // Saved value not in the listed models: keep it as a "(saved)" option so
  // the form does not silently rewrite it on the next render.
  const showSavedOutsideList =
    value && !allowEmpty && !result.models.includes(value);
  const showSavedAlongsideEmpty =
    value && allowEmpty && !result.models.includes(value);

  return (
    <select
      id={id}
      className={selectClass}
      value={value}
      onChange={e => onChange(e.target.value)}
    >
      {allowEmpty && <option value="">{emptyLabel}</option>}
      {(showSavedOutsideList || showSavedAlongsideEmpty) && (
        <option value={value}>{value} (saved, not in list)</option>
      )}
      {result.models.map(m => (
        <option key={m} value={m}>
          {m}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Endpoint <select>: the named destinations an operator has configured.
//
// Selecting one supersedes the provider beside it, because an endpoint states
// its own dialect. Forms that offer both disable the provider control while an
// endpoint is selected, rather than leaving a field that looks editable and is
// ignored.
// ---------------------------------------------------------------------------

interface LLMEndpointSelectProps {
  id?: string;
  value: string;
  onChange: (next: string) => void;
  /** Label for the "no endpoint, use a provider directly" option. */
  emptyLabel?: string;
  disabled?: boolean;
}

export function LLMEndpointSelect({
  id,
  value,
  onChange,
  emptyLabel = 'Direct to provider',
  disabled,
}: LLMEndpointSelectProps) {
  const [endpoints, setEndpoints] = useState<LLMEndpointItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listLLMEndpoints()
      .then(setEndpoints)
      .catch(() => setEndpoints([]))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <select id={id} className={selectClass} disabled value="" onChange={() => {}}>
        <option value="">Loading endpoints...</option>
      </select>
    );
  }

  // A saved selection missing from the list is the case that breaks calls at
  // runtime, so it is shown as an option rather than silently blanked.
  const known = new Set(endpoints.map(e => e.name));
  const showMissing = value && !known.has(value);

  return (
    <select
      id={id}
      className={selectClass}
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
    >
      <option value="">{emptyLabel}</option>
      {showMissing && <option value={value}>{value} (not configured)</option>}
      {endpoints.map(e => (
        <option key={e.name} value={e.name}>
          {e.name} ({e.dialect})
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Reasoning effort <select>.
// ---------------------------------------------------------------------------

/** Ascending, matching ``schemas.ReasoningEffort``. */
export const REASONING_EFFORTS = [
  'auto',
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
] as const;

interface ReasoningEffortSelectProps {
  id?: string;
  value: string;
  onChange: (next: string) => void;
  /** When set, prepend an empty option meaning "inherit". */
  inheritLabel?: string;
  disabled?: boolean;
}

export function ReasoningEffortSelect({
  id,
  value,
  onChange,
  inheritLabel,
  disabled,
}: ReasoningEffortSelectProps) {
  return (
    <select
      id={id}
      className={selectClass}
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
    >
      {inheritLabel && <option value="">{inheritLabel}</option>}
      {REASONING_EFFORTS.map(effort => (
        <option key={effort} value={effort}>
          {effort === 'auto' ? 'auto (provider default)' : effort}
        </option>
      ))}
    </select>
  );
}
