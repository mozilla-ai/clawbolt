import { useState, useCallback, useEffect } from 'react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Checkbox from '@/components/ui/checkbox';
import { Switch } from '@heroui/switch';
import Divider from '@/components/ui/divider';
import { toast } from '@/lib/toast';
import api from '@/api';
import type { ToolConfigEntry } from '@/types';

export default function ToolsPage() {
  const [tools, setTools] = useState<ToolConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [coreExpanded, setCoreExpanded] = useState(false);

  useEffect(() => {
    api.getToolConfig().then((res) => {
      setTools(res.tools);
      setLoading(false);
    }).catch(() => {
      setLoading(false);
    });
  }, []);

  const handleToggle = useCallback(async (name: string, enabled: boolean) => {
    setSaving(name);
    try {
      const res = await api.updateToolConfig([{ name, enabled }]);
      setTools(res.tools);
      toast.success(`${name} tool group ${enabled ? 'enabled' : 'disabled'}`);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(null);
    }
  }, []);

  if (loading) {
    return (
      <div>
        <h2 className="heading-page mb-6">Tools</h2>
        <Card>
          <p className="text-sm text-muted-foreground">Loading tool configuration...</p>
        </Card>
      </div>
    );
  }

  const coreTools = tools.filter((t) => t.category === 'core');
  const domainTools = tools.filter((t) => t.category === 'domain');

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
      <h2 className="heading-page mb-6">Tools</h2>
      <Card>
        <div className="grid gap-6">
          <div>
            <p className="text-sm text-muted-foreground mb-4">
              Configure which tool groups are available to your AI assistant.
            </p>
          </div>

          {groupNames.map((group) => (
            <div key={group}>
              <h3 className="text-sm font-medium mb-3">{group}</h3>
              <div className="grid gap-2">
                {(domainGroups[group] ?? []).map((tool) => (
                  <div key={tool.name} className="flex items-center justify-between py-2 px-3 rounded-md border border-border hover:bg-secondary-hover transition-all duration-150">
                    <div>
                      <span className="text-sm font-medium">{tool.name}</span>
                      {tool.description && (
                        <p className="text-xs text-muted-foreground">{tool.description}</p>
                      )}
                    </div>
                    <Switch
                      isSelected={tool.enabled}
                      isDisabled={saving === tool.name}
                      onValueChange={(val) => handleToggle(tool.name, val)}
                      size="sm"
                      aria-label={`Toggle ${tool.name}`}
                    />
                  </div>
                ))}
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
                <div className="grid gap-2 mt-3">
                  {coreTools.map((tool) => (
                    <div key={tool.name} className="flex items-center justify-between py-2 px-3 rounded-md bg-panel/50">
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
      </Card>
    </div>
  );
}
