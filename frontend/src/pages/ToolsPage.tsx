import { useState, useCallback, useEffect } from 'react';
import Card from '@/components/ui/card';
import { toast } from 'sonner';
import api from '@/api';
import type { ToolConfigEntry } from '@/types';

export default function ToolsPage() {
  const [tools, setTools] = useState<ToolConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);

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
        <h2 className="text-xl font-semibold mb-6">Tools</h2>
        <Card>
          <p className="text-sm text-muted-foreground">Loading tool configuration...</p>
        </Card>
      </div>
    );
  }

  const coreTools = tools.filter((t) => t.category === 'core');
  const domainTools = tools.filter((t) => t.category === 'domain');

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Tools</h2>
      <Card>
        <div className="grid gap-6">
          <div>
            <p className="text-sm text-muted-foreground mb-4">
              Configure which tool groups are available to your AI assistant.
              Core tools are always enabled. Domain-specific tools can be toggled on or off.
            </p>
          </div>

          {coreTools.length > 0 && (
            <div>
              <h3 className="text-sm font-medium mb-3">Core Tools (always enabled)</h3>
              <div className="grid gap-2">
                {coreTools.map((tool) => (
                  <div key={tool.name} className="flex items-center justify-between py-2 px-3 rounded bg-muted/50">
                    <div>
                      <span className="text-sm font-medium">{tool.name}</span>
                      {tool.description && (
                        <p className="text-xs text-muted-foreground">{tool.description}</p>
                      )}
                    </div>
                    <input
                      type="checkbox"
                      checked={true}
                      disabled={true}
                      className="w-4 h-4 rounded border-border text-primary opacity-50 cursor-not-allowed"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {domainTools.length > 0 && (
            <div>
              <h3 className="text-sm font-medium mb-3">Domain Tools</h3>
              <div className="grid gap-2">
                {domainTools.map((tool) => (
                  <div key={tool.name} className="flex items-center justify-between py-2 px-3 rounded border border-border">
                    <div>
                      <span className="text-sm font-medium">{tool.name}</span>
                      {tool.description && (
                        <p className="text-xs text-muted-foreground">{tool.description}</p>
                      )}
                    </div>
                    <input
                      type="checkbox"
                      checked={tool.enabled}
                      disabled={saving === tool.name}
                      onChange={(e) => handleToggle(tool.name, e.target.checked)}
                      className="w-4 h-4 rounded border-border text-primary focus:ring-primary"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
