import { useState } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Checkbox from '@/components/ui/checkbox';
import { Switch } from '@heroui/switch';
import { Divider } from '@heroui/divider';
import { toast } from '@/lib/toast';
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useOAuthDisconnect } from '@/hooks/queries';
import api from '@/api';
import type { ToolConfigEntryResponse, OAuthStatusEntry, SubToolEntryResponse } from '@/types';

// Map tool factory names to OAuth integration names.
const TOOL_OAUTH_MAP: Record<string, string> = {
  quickbooks: 'quickbooks',
  calendar: 'google_calendar',
};

export default function ToolsPage() {
  const { data, isPending } = useToolConfig();
  const updateMutation = useUpdateToolConfig();
  const { data: oauthData } = useOAuthStatus();
  const disconnectMutation = useOAuthDisconnect();
  const [coreExpanded, setCoreExpanded] = useState(false);
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
        toast.success(`${name} tool group ${enabled ? 'enabled' : 'disabled'}`),
      onError: (e) => toast.error(e.message),
    });
  };

  const handleSubToolToggle = (tool: ToolConfigEntryResponse, subToolName: string, enabled: boolean) => {
    // Compute the new disabled list from current sub_tools state
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
          toast.success(`${subToolName} ${enabled ? 'enabled' : 'disabled'}`),
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
      onSuccess: () => toast.success(`${integration} disconnected`),
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

  // Group domain tools by domain_group, sorted by domain_group_order
  const domainGroups: Record<string, ToolConfigEntryResponse[]> = {};
  const groupOrder: Record<string, number> = {};
  for (const tool of domainTools) {
    const group = tool.domain_group || 'Other';
    if (!domainGroups[group]) {
      domainGroups[group] = [];
      groupOrder[group] = tool.domain_group_order;
    }
    domainGroups[group].push(tool);
  }
  const groupNames = Object.keys(domainGroups).sort(
    (a, b) => (groupOrder[a] ?? 0) - (groupOrder[b] ?? 0) || a.localeCompare(b),
  );

  const renderSubTools = (tool: ToolConfigEntryResponse) => {
    if (!tool.sub_tools || tool.sub_tools.length === 0) return null;
    const isExpanded = expandedTools.has(tool.name);
    return (
      <>
        <Button
          variant="ghost"
          className="text-xs h-6 px-1.5 text-muted-foreground hover:text-foreground"
          onClick={() => toggleExpanded(tool.name)}
          aria-expanded={isExpanded}
        >
          <svg
            className={`w-3 h-3 transition-transform mr-1 ${isExpanded ? 'rotate-90' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          {tool.sub_tools.length} tools
        </Button>
        {isExpanded && (
          <div className="ml-4 mt-1 border-l border-border pl-3 space-y-1">
            {tool.sub_tools.map((st: SubToolEntryResponse) => (
              <div key={st.name} className="flex items-center justify-between py-1">
                <div className="flex-1 min-w-0">
                  <span className="text-xs font-mono">{st.name}</span>
                  {st.description && (
                    <p className="text-xs text-muted-foreground">{st.description}</p>
                  )}
                </div>
                <Switch
                  isSelected={st.enabled}
                  isDisabled={updateMutation.isPending || !tool.enabled}
                  onValueChange={(val) => handleSubToolToggle(tool, st.name, val)}
                  size="sm"
                  aria-label={`Toggle ${st.name}`}
                />
              </div>
            ))}
          </div>
        )}
      </>
    );
  };

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Tools</h2>
      <p className="text-sm text-muted-foreground mb-4">
        Configure which tool groups are available to your AI assistant.
        Expand a group to enable or disable individual tools.
      </p>

      <div className="grid gap-6">
        {groupNames.map((group) => (
          <div key={group}>
            <h3 className="text-sm font-medium mb-3">{group}</h3>
            <div className="divide-y divide-border">
              {(domainGroups[group] ?? []).map((tool) => {
                const oauthIntegration = TOOL_OAUTH_MAP[tool.name];
                const oauthEntry = oauthIntegration ? oauthMap[oauthIntegration] : undefined;
                return (
                  <div key={tool.name} className="py-2.5 px-1">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <span className="text-sm font-medium">{tool.name}</span>
                        {tool.description && (
                          <p className="text-xs text-muted-foreground">{tool.description}</p>
                        )}
                        {oauthEntry && oauthEntry.configured && (
                          <p className="text-xs mt-0.5">
                            {oauthEntry.connected ? (
                              <span className="text-green-600">Connected</span>
                            ) : (
                              <span className="text-yellow-600">Not connected</span>
                            )}
                          </p>
                        )}
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {oauthEntry && oauthEntry.configured && (
                          oauthEntry.connected ? (
                            <Button
                              variant="ghost"
                              className="text-xs h-7 px-2"
                              onClick={() => handleDisconnect(oauthIntegration!)}
                              disabled={disconnectMutation.isPending}
                            >
                              Disconnect
                            </Button>
                          ) : (
                            <Button
                              variant="ghost"
                              className="text-xs h-7 px-2 text-primary"
                              onClick={() => void handleConnect(oauthIntegration!)}
                              disabled={connectingIntegration === oauthIntegration}
                            >
                              {connectingIntegration === oauthIntegration ? 'Connecting...' : 'Connect'}
                            </Button>
                          )
                        )}
                        <Switch
                          isSelected={tool.enabled}
                          isDisabled={updateMutation.isPending}
                          onValueChange={(val) => handleToggle(tool.name, val)}
                          size="sm"
                          aria-label={`Toggle ${tool.name}`}
                        />
                      </div>
                    </div>
                    {renderSubTools(tool)}
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        {coreTools.length > 0 && (
          <div className="pt-4">
            <Divider className="mb-4" />
            <Button
              variant="ghost"
              className="w-full justify-start gap-2 text-muted-foreground hover:text-foreground font-medium"
              onClick={() => setCoreExpanded(!coreExpanded)}
              aria-expanded={coreExpanded}
            >
              <svg
                className={`w-4 h-4 transition-transform ${coreExpanded ? 'rotate-90' : ''}`}
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              Core Tools (always enabled)
            </Button>
            {coreExpanded && (
              <div className="divide-y divide-border mt-3">
                {coreTools.map((tool) => (
                  <div key={tool.name} className="py-2.5 px-1">
                    <div className="flex items-center justify-between">
                      <div>
                        <span className="text-sm font-medium">{tool.name}</span>
                        {tool.description && (
                          <p className="text-xs text-muted-foreground">{tool.description}</p>
                        )}
                      </div>
                      <Checkbox checked disabled />
                    </div>
                    {renderSubTools(tool)}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
