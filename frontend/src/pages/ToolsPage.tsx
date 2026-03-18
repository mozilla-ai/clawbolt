import { useState } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Checkbox from '@/components/ui/checkbox';
import { Switch } from '@heroui/switch';
import { Divider } from '@heroui/divider';
import { toast } from '@/lib/toast';
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useOAuthDisconnect } from '@/hooks/queries';
import api from '@/api';
import type { ToolConfigEntry, OAuthStatusEntry } from '@/types';

// Map tool factory names to OAuth integration names.
const TOOL_OAUTH_MAP: Record<string, string> = {
  quickbooks: 'quickbooks',
};

export default function ToolsPage() {
  const { data, isPending } = useToolConfig();
  const updateMutation = useUpdateToolConfig();
  const { data: oauthData } = useOAuthStatus();
  const disconnectMutation = useOAuthDisconnect();
  const [coreExpanded, setCoreExpanded] = useState(false);
  const [connectingIntegration, setConnectingIntegration] = useState<string | null>(null);

  const tools = data?.tools ?? [];
  const oauthMap: Record<string, OAuthStatusEntry> = {};
  for (const entry of oauthData?.integrations ?? []) {
    oauthMap[entry.integration] = entry;
  }

  const handleToggle = (name: string, enabled: boolean) => {
    updateMutation.mutate([{ name, enabled }], {
      onSuccess: () =>
        toast.success(`${name} tool group ${enabled ? 'enabled' : 'disabled'}`),
      onError: (e) => toast.error(e.message),
    });
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
        <h2 className="text-xl font-semibold mb-6">Tools</h2>
        <Card>
          <p className="text-sm text-muted-foreground">Loading tool configuration...</p>
        </Card>
      </div>
    );
  }

  const coreTools = tools.filter((t: ToolConfigEntry) => t.category === 'core');
  const domainTools = tools.filter((t: ToolConfigEntry) => t.category === 'domain');

  // Group domain tools by domain_group, sorted by domain_group_order
  const domainGroups: Record<string, ToolConfigEntry[]> = {};
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

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Tools</h2>
      <p className="text-sm text-muted-foreground mb-4">
        Configure which tool groups are available to your AI assistant.
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
                  <div key={tool.name} className="flex items-center justify-between py-2.5 px-1 gap-3">
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
                  <div key={tool.name} className="flex items-center justify-between py-2.5 px-1">
                    <div>
                      <span className="text-sm font-medium">{tool.name}</span>
                      {tool.description && (
                        <p className="text-xs text-muted-foreground">{tool.description}</p>
                      )}
                    </div>
                    <Checkbox checked disabled />
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
