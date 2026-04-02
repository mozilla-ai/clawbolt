import { useState } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { Switch } from '@heroui/switch';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useOAuthDisconnect, useCalendarList, useCalendarConfig, useUpdateCalendarConfig } from '@/hooks/queries';
import api from '@/api';
import type { ToolConfigEntryResponse, OAuthStatusEntry, SubToolEntryResponse } from '@/types';

// Map tool factory names to OAuth integration names.
const TOOL_OAUTH_MAP: Record<string, string> = {
  quickbooks: 'quickbooks',
  calendar: 'google_calendar',
};

// Human-readable display names for tool factories.
const DISPLAY_NAMES: Record<string, string> = {
  quickbooks: 'QuickBooks',
  calendar: 'Google Calendar',
  supplier_pricing: 'Pricing Tools',
  workspace: 'Workspace',
  profile: 'Profile',
  memory: 'Memory',
  messaging: 'Messaging',
  file: 'File Storage',
  heartbeat: 'Heartbeat',
  permissions: 'Permissions',
};

// Human-readable sub-tool display names.
const SUB_TOOL_NAMES: Record<string, string> = {
  qb_query: 'Query entities',
  qb_create: 'Create entities',
  qb_update: 'Update entities',
  qb_send: 'Send documents',
  calendar_list_calendars: 'List calendars',
  calendar_list_events: 'List events',
  calendar_create_event: 'Create events',
  calendar_update_event: 'Update events',
  calendar_delete_event: 'Delete events',
  calendar_check_availability: 'Check availability',
  read_file: 'Read files',
  write_file: 'Write files',
  edit_file: 'Edit files',
  delete_file: 'Delete files',
  upload_to_storage: 'Upload files',
  organize_file: 'Organize files',
  get_heartbeat: 'Read heartbeat',
  update_heartbeat: 'Update heartbeat',
  send_reply: 'Send replies',
  send_media_reply: 'Send media',
  update_permission: 'Change permissions',
  supplier_search_products: 'Search products',
};

function displayName(name: string): string {
  return DISPLAY_NAMES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

function subToolDisplayName(name: string): string {
  return SUB_TOOL_NAMES[name] ?? name.split('_').join(' ');
}

const PERMISSION_LABELS: Record<string, { label: string; className: string; tooltip: string }> = {
  auto: {
    label: 'Runs freely',
    className: 'text-success',
    tooltip: 'Used automatically, no approval needed. To change, just tell your assistant in chat.',
  },
  ask: {
    label: 'Asks first',
    className: 'text-warning',
    tooltip: 'Your assistant asks for your OK before using this. To change, just tell your assistant in chat.',
  },
  deny: {
    label: 'Blocked',
    className: 'text-danger',
    tooltip: 'Your assistant will not use this. To unblock, just tell your assistant in chat.',
  },
};

function PermissionBadge({ level }: { level: string }) {
  const info = PERMISSION_LABELS[level] ?? PERMISSION_LABELS['ask']!;
  return (
    <Tooltip content={info.tooltip} delay={400} closeDelay={0}>
      <span className={`text-[10px] ${info.className} shrink-0 cursor-default`}>
        {info.label}
      </span>
    </Tooltip>
  );
}

export default function ToolsPage() {
  const { data, isPending } = useToolConfig();
  const updateMutation = useUpdateToolConfig();
  const { data: oauthData } = useOAuthStatus();
  const disconnectMutation = useOAuthDisconnect();
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [connectingIntegration, setConnectingIntegration] = useState<string | null>(null);

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

  const handleSubToolToggle = (tool: ToolConfigEntryResponse, subToolName: string, enabled: boolean) => {
    const currentlyDisabled = (tool.sub_tools ?? [])
      .filter((st) => !st.enabled)
      .map((st) => st.name);

    let newDisabled: string[];
    if (enabled) {
      newDisabled = currentlyDisabled.filter((n) => n !== subToolName);
    } else {
      newDisabled = [...currentlyDisabled, subToolName];
    }

    updateMutation.mutate(
      [{ name: tool.name, enabled: tool.enabled, disabled_sub_tools: newDisabled }],
      {
        onSuccess: () =>
          toast.success(`${subToolDisplayName(subToolName)} ${enabled ? 'enabled' : 'disabled'}`),
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

  if (isPending && !data) {
    return (
      <div>
        <h2 className="text-xl font-semibold font-display mb-6">Tools</h2>
        <Card>
          <p className="text-sm text-muted-foreground">Loading tool configuration...</p>
        </Card>
      </div>
    );
  }

  const domainTools = tools.filter((t: ToolConfigEntryResponse) => t.category === 'domain');

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Tools</h2>

      {/* Integrations */}
      {domainTools.length > 0 && (
        <section>
          <h3 className="text-sm font-medium mb-3">Integrations</h3>
          <div className="grid gap-3">
            {domainTools.map((tool) => {
              const oauthIntegration = TOOL_OAUTH_MAP[tool.name];
              const oauthEntry = oauthIntegration ? oauthMap[oauthIntegration] : undefined;
              const isConnected = oauthEntry?.connected ?? false;
              const isConfigured = oauthEntry?.configured ?? false;

              return (
                <Card key={tool.name} className={!isConfigured ? 'opacity-50' : undefined}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{displayName(tool.name)}</span>
                        {isConfigured ? (
                          <span className="inline-flex items-center gap-1.5 text-xs">
                            <span className={`size-1.5 rounded-full inline-block shrink-0 ${
                              isConnected ? 'bg-success' : 'bg-warning'
                            }`} />
                            {isConnected ? 'Connected' : 'Not connected'}
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                            <span className="size-1.5 rounded-full inline-block shrink-0 bg-default-300" />
                            Not configured
                          </span>
                        )}
                      </div>
                      {tool.description && (
                        <p className="text-xs text-muted-foreground mt-1">{tool.description}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      {isConfigured && (
                        isConnected ? (
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => handleDisconnect(oauthIntegration!)}
                            disabled={disconnectMutation.isPending}
                          >
                            Disconnect
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() => void handleConnect(oauthIntegration!)}
                            disabled={connectingIntegration === oauthIntegration}
                            isLoading={connectingIntegration === oauthIntegration}
                          >
                            Connect
                          </Button>
                        )
                      )}
                    </div>
                  </div>

                  {/* Enable/disable toggle, only when connected */}
                  {isConnected && (
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
                      onSubToolToggle={handleSubToolToggle}
                      isUpdating={updateMutation.isPending}
                    />
                  )}
                </Card>
              );
            })}
          </div>
        </section>
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
                          <PermissionBadge level={subToolPermissions[toolName] ?? 'ask'} />
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
  onSubToolToggle,
  isUpdating,
}: {
  tool: ToolConfigEntryResponse;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onSubToolToggle: (tool: ToolConfigEntryResponse, subToolName: string, enabled: boolean) => void;
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
                <div className="flex items-center gap-2">
                  <span className="text-xs">{subToolDisplayName(st.name)}</span>
                  <PermissionBadge level={st.permission_level} />
                </div>
                {st.description && (
                  <p className="text-xs text-muted-foreground">{st.description}</p>
                )}
              </div>
              <Switch
                isSelected={st.enabled}
                isDisabled={isUpdating}
                onValueChange={(val) => onSubToolToggle(tool, st.name, val)}
                size="sm"
                aria-label={`Toggle ${subToolDisplayName(st.name)}`}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
