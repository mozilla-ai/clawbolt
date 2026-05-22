import { useState } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { Switch } from '@heroui/switch';
import { toast } from '@/lib/toast';
import { displayName, subToolDisplayName, getToolOAuthStatus } from '@/lib/tool-utils';
import { IntegrationIcon } from '@/components/integration-icons';
import PermissionSelector, { PERM_OPTIONS, type PermLevel } from '@/components/PermissionSelector';
import Field from '@/components/ui/field';
import Input from '@/components/ui/input';

const PERM_LEVEL_CLASSNAMES: Record<PermLevel, string> = {
  always: 'text-success',
  ask: 'text-warning',
  never: 'text-danger',
};

function PermissionLevelLabel({ level }: { level: string }) {
  const normalized = (level === 'always' || level === 'ask' || level === 'never'
    ? level
    : 'ask') as PermLevel;
  const label = PERM_OPTIONS.find((o) => o.value === normalized)?.label ?? normalized;
  return (
    <span className={`text-[10px] shrink-0 ${PERM_LEVEL_CLASSNAMES[normalized]}`}>
      {label}
    </span>
  );
}
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useOAuthDisconnect, useCalendarList, useCalendarConfig, useUpdateCalendarConfig } from '@/hooks/queries';
import api from '@/api';
import type { ToolConfigEntryResponse, OAuthStatusEntry, SubToolEntryResponse } from '@/types';

// Integrations that use web-app credential input instead of OAuth.
// These show a Connect button that opens a credential form in the web UI.
const WEB_CONNECT_INTEGRATIONS = new Set(['appfolio_vendor', 'servicetitan']);

export default function ToolsPage() {
  const { data, isPending } = useToolConfig();
  const updateMutation = useUpdateToolConfig();
  const { data: oauthData } = useOAuthStatus();
  const disconnectMutation = useOAuthDisconnect();
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [connectingIntegration, setConnectingIntegration] = useState<string | null>(null);

  // State for web-connect credential forms (AppFolio, ServiceTitan)
  const [showAppfolioForm, setShowAppfolioForm] = useState(false);
  const [appfolioMagicLink, setAppfolioMagicLink] = useState('');
  const [appfolioConnecting, setAppfolioConnecting] = useState(false);
  const [showServiceTitanForm, setShowServiceTitanForm] = useState(false);
  const [stTenantId, setStTenantId] = useState('');
  const [stClientId, setStClientId] = useState('');
  const [stClientSecret, setStClientSecret] = useState('');
  const [stConnecting, setStConnecting] = useState(false);

  const tools = data?.tools ?? [];
  const oauthMap: Record<string, OAuthStatusEntry> = {};
  for (const entry of oauthData?.integrations ?? []) {
    oauthMap[entry.integration] = entry;
  }

  const toggleExpanded = (name: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  const handleToggle = (name: string, enabled: boolean) => {
    updateMutation.mutate([{ name, enabled }], {
      onSuccess: () =>
        toast.success(`${displayName(name)} ${enabled ? 'enabled' : 'disabled'}`),
      onError: (e) => toast.error(e.message),
    });
  };

  const handleSubToolPermissionChange = (
    tool: ToolConfigEntryResponse,
    subToolName: string,
    level: PermLevel,
  ) => {
    updateMutation.mutate(
      [
        {
          name: tool.name,
          enabled: tool.enabled,
          sub_tools: [{ name: subToolName, permission_level: level }],
        },
      ],
      {
        onSuccess: () =>
          toast.success(`${subToolDisplayName(subToolName)} set to ${level}`),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const handleConnect = async (integration: string) => {
    setConnectingIntegration(integration);
    try {
      const { url } = await api.getOAuthAuthorizeUrl(integration);
      window.location.href = url;
    } catch (e) {
      const err = e instanceof Error ? e.message : 'Failed to start authorization';
      toast.error(err);
      setConnectingIntegration(null);
    }
  };

  const handleDisconnect = (integration: string) => {
    disconnectMutation.mutate(integration, {
      onSuccess: () => toast.success(`Disconnected`),
      onError: (e) => toast.error(e.message),
    });
  };

  const handleAppfolioConnect = async () => {
    if (!appfolioMagicLink.trim()) {
      toast.error('Please enter the magic link from your AppFolio email.');
      return;
    }
    setAppfolioConnecting(true);
    try {
      await api.connectAppfolio(appfolioMagicLink.trim());
      toast.success('AppFolio Vendor Portal connected successfully.');
      setShowAppfolioForm(false);
      setAppfolioMagicLink('');
    } catch (e) {
      const err = e instanceof Error ? e.message : 'Failed to connect AppFolio';
      toast.error(err);
    } finally {
      setAppfolioConnecting(false);
    }
  };

  const handleServiceTitanConnect = async () => {
    if (!stTenantId.trim() || !stClientId.trim() || !stClientSecret.trim()) {
      toast.error('Tenant ID, Client ID, and Client Secret are all required.');
      return;
    }
    setStConnecting(true);
    try {
      await api.connectServiceTitan(stTenantId.trim(), stClientId.trim(), stClientSecret.trim());
      toast.success('ServiceTitan connected successfully.');
      setShowServiceTitanForm(false);
      setStTenantId('');
      setStClientId('');
      setStClientSecret('');
    } catch (e) {
      const err = e instanceof Error ? e.message : 'Failed to connect ServiceTitan';
      toast.error(err);
    } finally {
      setStConnecting(false);
    }
  };

  if (isPending && !data) {
    return (
      <div>
        <h2 className="text-xl font-semibold font-display mb-6">Integrations</h2>
        <Card>
          <p className="text-sm text-muted-foreground">Loading tool configuration...</p>
        </Card>
      </div>
    );
  }

  // Render any tool the backend marks as part of an integrations group
  // (``category === 'domain'``) plus any OAuth-backed tool that is rendered
  // as always-on in Settings (e.g. Google Drive). The latter has no toggle
  // but still needs Connect / Disconnect.
  const integrationTools = tools.filter(
    (t: ToolConfigEntryResponse) => t.category === 'domain' || !!t.oauth_name,
  );

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Tools</h2>

      {integrationTools.length > 0 && (
        <section>
          <div className="grid gap-3">
            {integrationTools.map((tool) => {
              const oauthIntegration = tool.oauth_name;
              const { needsOAuth, isConfigured, isConnected } = getToolOAuthStatus(oauthIntegration, oauthMap, tool.configured);
              const isWebConnect = WEB_CONNECT_INTEGRATIONS.has(tool.name);

              return (
                <Card key={tool.name}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-3 flex-1 min-w-0">
                      <IntegrationIcon name={tool.name} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium">{displayName(tool.name)}</span>
                          {isConfigured ? (
                            <span className="inline-flex items-center gap-1.5 text-xs">
                              <span className={`size-1.5 rounded-full inline-block shrink-0 ${
                                isConnected ? 'bg-success' : 'bg-warning'
                              }`} />
                              {(needsOAuth || isWebConnect) ? (isConnected ? 'Connected' : 'Not connected') : 'Available'}
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                              <span className="size-1.5 rounded-full inline-block shrink-0 bg-default-300" />
                              Not connected
                            </span>
                          )}
                        </div>
                        {tool.description && (
                          <p className="text-xs text-muted-foreground mt-1">{tool.description}</p>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      {needsOAuth && isConnected && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => handleDisconnect(oauthIntegration)}
                          disabled={disconnectMutation.isPending}
                        >
                          Disconnect
                        </Button>
                      )}
                      {needsOAuth && !isConnected && (
                        <Button
                          size="sm"
                          onClick={() => void handleConnect(oauthIntegration)}
                          disabled={connectingIntegration === oauthIntegration}
                          isLoading={connectingIntegration === oauthIntegration}
                        >
                          Connect
                        </Button>
                      )}
                      {isWebConnect && isConnected && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => void handleDisconnect('', tool.name)}
                        >
                          Disconnect
                        </Button>
                      )}
                      {isWebConnect && !isConnected && (
                        <Button
                          size="sm"
                          onClick={() => {
                            if (tool.name === 'appfolio_vendor') setShowAppfolioForm(true);
                            else if (tool.name === 'servicetitan') setShowServiceTitanForm(true);
                          }}
                        >
                          Connect
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* Enable/disable toggle. Always-enabled integrations
                      (e.g. Google Drive) skip the toggle because the backend
                      silently ignores attempts to disable them. */}
                  {isConnected && !tool.always_enabled && (
                    <div className="flex items-center justify-between mt-3 pt-3 border-t border-border">
                      <span className="text-xs text-muted-foreground">
                        {tool.enabled ? 'Available to assistant' : 'Disabled'}
                      </span>
                      <Switch
                        isSelected={tool.enabled}
                        isDisabled={updateMutation.isPending}
                        onValueChange={(val) => handleToggle(tool.name, val)}
                        size="sm"
                        aria-label={`Toggle ${displayName(tool.name)}`}
                      />
                    </div>
                  )}

                  {/* Calendar picker */}
                  {isConnected && tool.enabled && tool.name === 'calendar' && (
                    <CalendarPicker subToolPermissions={
                      Object.fromEntries((tool.sub_tools ?? []).map((st) => [st.name, st.permission_level]))
                    } />
                  )}

                  {/* Sub-tools (expandable) */}
                  {isConnected && tool.enabled && (
                    <SubToolList
                      tool={tool}
                      isExpanded={expandedTools.has(tool.name)}
                      onToggleExpand={() => toggleExpanded(tool.name)}
                      onPermissionChange={handleSubToolPermissionChange}
                      isUpdating={updateMutation.isPending}
                    />
                  )}
                </Card>
              );
            })}
          </div>
        </section>
      )}

      {/* AppFolio connect modal */}
      {showAppfolioForm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
          onClick={(e) => {
            // Close when clicking the overlay backdrop, not the modal content
            if (e.target === e.currentTarget) setShowAppfolioForm(false);
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="appfolio-modal-title"
        >
          <div className="bg-card border border-border rounded-lg shadow-lg w-full max-w-sm mx-4 p-5 animate-message-in">
            <h3 id="appfolio-modal-title" className="text-base font-semibold font-display text-foreground">
              Connect AppFolio Vendor Portal
            </h3>
            <div className="mt-3 space-y-3">
              <p className="text-xs text-muted-foreground">
                Paste the magic link from your AppFolio email. Open vendor.appfolio.com,
                request a sign-in link, and copy the token from the URL.
              </p>
              <Field label="Magic Link URL or Token">
                <Input
                  value={appfolioMagicLink}
                  onChange={(e) => setAppfolioMagicLink(e.target.value)}
                  placeholder="e.g. https://vendor.appfolio.com/?magic_link_token=..."
                />
              </Field>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowAppfolioForm(false)}
                disabled={appfolioConnecting}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleAppfolioConnect}
                disabled={appfolioConnecting}
                isLoading={appfolioConnecting}
              >
                Connect
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ServiceTitan connect modal */}
      {showServiceTitanForm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
          onClick={(e) => {
            // Close when clicking the overlay backdrop, not the modal content
            if (e.target === e.currentTarget) setShowServiceTitanForm(false);
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="st-modal-title"
        >
          <div className="bg-card border border-border rounded-lg shadow-lg w-full max-w-sm mx-4 p-5 animate-message-in">
            <h3 id="st-modal-title" className="text-base font-semibold font-display text-foreground">
              Connect ServiceTitan
            </h3>
            <div className="mt-3 space-y-3">
              <p className="text-xs text-muted-foreground">
                Enter your ServiceTitan credentials from Settings > Integrations >
                API Application Access.
              </p>
              <Field label="Tenant ID">
                <Input
                  value={stTenantId}
                  onChange={(e) => setStTenantId(e.target.value)}
                  placeholder="e.g. 1234567"
                />
              </Field>
              <Field label="Client ID">
                <Input
                  value={stClientId}
                  onChange={(e) => setStClientId(e.target.value)}
                  placeholder="e.g. my-client-id"
                />
              </Field>
              <Field label="Client Secret">
                <Input
                  type="password"
                  value={stClientSecret}
                  onChange={(e) => setStClientSecret(e.target.value)}
                  placeholder="Enter client secret"
                />
              </Field>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowServiceTitanForm(false)}
                disabled={stConnecting}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleServiceTitanConnect}
                disabled={stConnecting}
                isLoading={stConnecting}
              >
                Connect
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Per-calendar tool names that can be individually toggled per calendar.
const PER_CALENDAR_TOOLS = [
  'calendar_list_events',
  'calendar_create_event',
  'calendar_update_event',
  'calendar_delete_event',
] as const;

// Write tools that are auto-disabled on read-only calendars.
const WRITE_TOOLS = new Set([
  'calendar_create_event',
  'calendar_update_event',
  'calendar_delete_event',
]);

const READ_ONLY_ROLES = new Set(['reader', 'freeBusyReader']);

function CalendarPicker({ subToolPermissions }: { subToolPermissions: Record<string, string> }) {
  const { data: calendars, isPending: isLoadingCalendars } = useCalendarList();
  const { data: config } = useCalendarConfig();
  const updateConfig = useUpdateCalendarConfig();
  const [expandedCals, setExpandedCals] = useState<Set<string>>(new Set());

  const calendarList = calendars?.calendars ?? [];
  const configMap = new Map(
    (config?.calendars ?? []).map((c) => [c.calendar_id, c]),
  );

  if (isLoadingCalendars) {
    return (
      <div className="mt-3 pt-3 border-t border-border">
        <p className="text-xs text-muted-foreground">Loading calendars...</p>
      </div>
    );
  }

  if (calendarList.length === 0) return null;

  const save = (
    next: Array<{ calendar_id: string; display_name: string; disabled_tools: string[]; access_role: string }>,
    message: string,
  ) => {
    updateConfig.mutate(
      { calendars: next },
      {
        onSuccess: () => toast.success(message),
        onError: (err) => toast.error(err.message),
      },
    );
  };

  const handleToggle = (calId: string, calName: string, accessRole: string, checked: boolean) => {
    const current = config?.calendars ?? [];
    if (checked) {
      // Auto-disable write tools for read-only calendars
      const autoDisabled = READ_ONLY_ROLES.has(accessRole)
        ? [...WRITE_TOOLS]
        : [];
      save(
        [...current, { calendar_id: calId, display_name: calName, disabled_tools: autoDisabled, access_role: accessRole }],
        `Calendar enabled: ${calName}`,
      );
    } else {
      save(
        current.filter((c) => c.calendar_id !== calId),
        `Calendar disabled: ${calName}`,
      );
    }
  };

  const toggleCalExpanded = (calId: string) => {
    setExpandedCals((prev) => {
      const next = new Set(prev);
      if (next.has(calId)) next.delete(calId);
      else next.add(calId);
      return next;
    });
  };

  const handleToolToggle = (calId: string, toolName: string, enabled: boolean) => {
    const current = config?.calendars ?? [];
    const entry = current.find((c) => c.calendar_id === calId);
    if (!entry) return;

    const disabled = entry.disabled_tools ?? [];
    const newDisabled = enabled
      ? disabled.filter((t) => t !== toolName)
      : [...disabled, toolName];

    save(
      current.map((c) =>
        c.calendar_id === calId ? { ...c, disabled_tools: newDisabled, access_role: c.access_role ?? '' } : { ...c, access_role: c.access_role ?? '' },
      ),
      `${subToolDisplayName(toolName)} ${enabled ? 'enabled' : 'disabled'}`,
    );
  };

  return (
    <div className="mt-3 pt-3 border-t border-border">
      <span className="block text-xs text-muted-foreground mb-1.5">
        Enabled calendars (the assistant will only see these)
      </span>
      <div className="space-y-2">
        {calendarList.map((cal) => {
          const entry = configMap.get(cal.id);
          const isEnabled = !!entry;
          const isExpanded = expandedCals.has(cal.id);
          const disabled = new Set(entry?.disabled_tools ?? []);
          const isReadOnly = READ_ONLY_ROLES.has(cal.access_role ?? '');

          return (
            <div key={cal.id}>
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-2 text-sm cursor-pointer flex-1 min-w-0">
                  <input
                    type="checkbox"
                    checked={isEnabled}
                    disabled={updateConfig.isPending}
                    onChange={(e) => handleToggle(cal.id, cal.summary, cal.access_role ?? '', e.target.checked)}
                    className="rounded border-border shrink-0"
                  />
                  <span className="truncate">
                    {cal.summary}{cal.primary ? ' (primary)' : ''}
                  </span>
                  {isReadOnly && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground shrink-0">
                      read-only
                    </span>
                  )}
                </label>
                {isEnabled && (
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0"
                    onClick={() => toggleCalExpanded(cal.id)}
                    aria-expanded={isExpanded}
                  >
                    <svg
                      className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                    {disabled.size === 0 ? 'Full access' : `${PER_CALENDAR_TOOLS.length - disabled.size}/${PER_CALENDAR_TOOLS.length}`}
                  </button>
                )}
              </div>
              {isEnabled && isExpanded && (
                <div className="mt-1.5 ml-6 pl-3 border-l border-border space-y-1">
                  {PER_CALENDAR_TOOLS.map((toolName) => {
                    const isWriteTool = WRITE_TOOLS.has(toolName);
                    const lockedByRole = isReadOnly && isWriteTool;
                    return (
                      <div key={toolName} className="flex items-center justify-between gap-3 py-0.5">
                        <div className="flex items-center gap-2">
                          <span className={`text-xs ${lockedByRole ? 'text-muted-foreground' : ''}`}>
                            {subToolDisplayName(toolName)}
                            {lockedByRole ? ' (read-only)' : ''}
                          </span>
                          <PermissionLevelLabel level={subToolPermissions[toolName] ?? 'ask'} />
                        </div>
                        <Switch
                          isSelected={!disabled.has(toolName)}
                          isDisabled={updateConfig.isPending || lockedByRole}
                          onValueChange={(val) => handleToolToggle(cal.id, toolName, val)}
                          size="sm"
                          aria-label={`Toggle ${subToolDisplayName(toolName)} for ${cal.summary}`}
                        />
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Per-calendar tool names are managed in CalendarPicker, not in SubToolList.
const PER_CALENDAR_TOOL_SET = new Set<string>(PER_CALENDAR_TOOLS);

function SubToolList({
  tool,
  isExpanded,
  onToggleExpand,
  onPermissionChange,
  isUpdating,
}: {
  tool: ToolConfigEntryResponse;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onPermissionChange: (tool: ToolConfigEntryResponse, subToolName: string, level: PermLevel) => void;
  isUpdating: boolean;
}) {
  if (!tool.sub_tools || tool.sub_tools.length === 0) return null;

  // For the calendar tool, filter out per-calendar tools (handled in CalendarPicker).
  const visibleSubTools = tool.name === 'calendar'
    ? tool.sub_tools.filter((st) => !PER_CALENDAR_TOOL_SET.has(st.name))
    : tool.sub_tools;

  if (visibleSubTools.length === 0) return null;

  return (
    <div className="mt-2">
      <button
        type="button"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
        onClick={onToggleExpand}
        aria-expanded={isExpanded}
      >
        <svg
          className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        {visibleSubTools.length} global {visibleSubTools.length === 1 ? 'capability' : 'capabilities'}
      </button>
      {isExpanded && (
        <div className="mt-2 pl-4 border-l border-border space-y-1.5">
          {visibleSubTools.map((st: SubToolEntryResponse) => (
            <div key={st.name} className="flex items-center justify-between gap-3 py-0.5">
              <div className="flex-1 min-w-0">
                <span className="text-xs">{subToolDisplayName(st.name)}</span>
                {st.description && (
                  <p className="text-xs text-muted-foreground">{st.description}</p>
                )}
              </div>
              <PermissionSelector
                toolName={subToolDisplayName(st.name)}
                level={st.permission_level as PermLevel}
                onChange={(level) => onPermissionChange(tool, st.name, level)}
                disabled={isUpdating}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
