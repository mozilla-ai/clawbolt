import { useState } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { Switch } from '@heroui/switch';
import { toast } from '@/lib/toast';
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useOAuthDisconnect } from '@/hooks/queries';
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
  workspace: 'Workspace',
  profile: 'Profile',
  memory: 'Memory',
  messaging: 'Messaging',
  file: 'File Storage',
  heartbeat: 'Heartbeat',
};

// Human-readable sub-tool display names.
const SUB_TOOL_NAMES: Record<string, string> = {
  qb_query: 'Query entities',
  qb_create: 'Create entities',
  qb_update: 'Update entities',
  qb_send: 'Send documents',
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
};

function displayName(name: string): string {
  return DISPLAY_NAMES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

function subToolDisplayName(name: string): string {
  return SUB_TOOL_NAMES[name] ?? name.split('_').join(' ');
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

  const coreTools = tools.filter((t: ToolConfigEntryResponse) => t.category === 'core');
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
                <Card key={tool.name}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{displayName(tool.name)}</span>
                        {isConfigured && (
                          <span className="inline-flex items-center gap-1.5 text-xs">
                            <span className={`size-1.5 rounded-full inline-block shrink-0 ${
                              isConnected ? 'bg-success' : 'bg-warning'
                            }`} />
                            {isConnected ? 'Connected' : 'Not connected'}
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

      {/* Core Tools */}
      {coreTools.length > 0 && (
        <section className={domainTools.length > 0 ? 'mt-8' : ''}>
          <h3 className="text-sm font-medium mb-3">Core Tools</h3>
          <Card>
            <div className="divide-y divide-border -my-1">
              {coreTools.map((tool) => (
                <div key={tool.name} className="flex items-center justify-between py-2.5 first:pt-0 last:pb-0">
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-medium">{displayName(tool.name)}</span>
                    {tool.description && (
                      <p className="text-xs text-muted-foreground mt-0.5">{tool.description}</p>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground shrink-0 ml-4">Always on</span>
                </div>
              ))}
            </div>
          </Card>
        </section>
      )}
    </div>
  );
}

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
        {tool.sub_tools.length} capabilities
      </button>
      {isExpanded && (
        <div className="mt-2 pl-4 border-l border-border space-y-1.5">
          {tool.sub_tools.map((st: SubToolEntryResponse) => (
            <div key={st.name} className="flex items-center justify-between gap-3 py-0.5">
              <div className="flex-1 min-w-0">
                <span className="text-xs">{subToolDisplayName(st.name)}</span>
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
