import { useState, useCallback, useMemo } from 'react';
import { Spinner } from '@heroui/spinner';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useToolConfig, usePermissions, useUpdatePermissions } from '@/hooks/queries';
import { displayName, subToolDisplayName } from '@/lib/tool-utils';
import type { ToolConfigEntryResponse, SubToolEntryResponse } from '@/types';

type PermLevel = 'always' | 'ask' | 'deny';

const PERM_OPTIONS: { value: PermLevel; label: string }[] = [
  { value: 'always', label: 'Runs freely' },
  { value: 'ask', label: 'Asks first' },
  { value: 'deny', label: 'Blocked' },
];

const PERM_ACTIVE_STYLES: Record<PermLevel, string> = {
  always: 'bg-muted text-success font-medium',
  ask: 'bg-muted text-warning font-medium',
  deny: 'bg-muted text-danger font-medium',
};

export default function PermissionsPage() {
  const { data: toolData, isPending: toolsPending, isError } = useToolConfig();
  const { data: permData, isPending: permsPending } = usePermissions();
  const updateMutation = useUpdatePermissions();
  const [collapsedTools, setCollapsedTools] = useState<Set<string>>(new Set());

  const tools = toolData?.tools ?? [];
  const rawContent = permData?.content ?? '';

  const coreTools = useMemo(
    () => tools.filter((t) => t.category === 'core' && (t.sub_tools?.length ?? 0) > 0),
    [tools],
  );
  const domainTools = useMemo(
    () => tools.filter((t) => t.category === 'domain' && (t.sub_tools?.length ?? 0) > 0),
    [tools],
  );

  const toggleCollapsed = (name: string) => {
    setCollapsedTools((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const handlePermissionChange = useCallback(
    async (subToolName: string, level: string) => {
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(rawContent) as Record<string, unknown>;
      } catch {
        parsed = {};
      }

      const toolPerms = { ...((parsed.tools as Record<string, string>) ?? {}) };
      toolPerms[subToolName] = level;
      parsed = { ...parsed, tools: toolPerms };

      const content = JSON.stringify(parsed, null, 2);
      try {
        await updateMutation.mutateAsync(
          { content },
          {
            onSuccess: () => {
              const label = PERM_OPTIONS.find((o) => o.value === level)?.label ?? level;
              toast.success(`${subToolDisplayName(subToolName)}: ${label}`);
            },
            onError: (e) => toast.error(e.message),
          },
        );
      } catch {
        // handled by onError
      }
    },
    [rawContent, updateMutation],
  );

  if (toolsPending && !toolData) {
    return (
      <div className="flex justify-center py-12">
        <Spinner color="primary" size="md" aria-label="Loading" />
      </div>
    );
  }

  if (isError && !toolData) {
    return <p className="text-sm text-danger py-4">Failed to load permissions.</p>;
  }

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold font-display">Permissions</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Control which actions your assistant can take freely, which require approval, and which
          are blocked.
        </p>
      </div>

      {coreTools.length > 0 && (
        <section className="mb-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {coreTools.map((tool) => (
              <ToolPermissionCard
                key={tool.name}
                tool={tool}
                isExpanded={!collapsedTools.has(tool.name)}
                onToggleExpand={() => toggleCollapsed(tool.name)}
                onPermissionChange={handlePermissionChange}
                isUpdating={updateMutation.isPending || permsPending}
              />
            ))}
          </div>
        </section>
      )}

      {domainTools.length > 0 && (
        <section className="mb-4">
          <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">Integrations</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {domainTools.map((tool) => (
              <ToolPermissionCard
                key={tool.name}
                tool={tool}
                isExpanded={!collapsedTools.has(tool.name)}
                onToggleExpand={() => toggleCollapsed(tool.name)}
                onPermissionChange={handlePermissionChange}
                isUpdating={updateMutation.isPending || permsPending}
              />
            ))}
          </div>
        </section>
      )}

    </div>
  );
}

function ToolPermissionCard({
  tool,
  isExpanded,
  onToggleExpand,
  onPermissionChange,
  isUpdating,
}: {
  tool: ToolConfigEntryResponse;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onPermissionChange: (toolName: string, level: string) => void;
  isUpdating: boolean;
}) {
  const subTools = tool.sub_tools ?? [];

  return (
    <div className="rounded-[var(--radius-lg)] border border-border bg-card p-3">
      <button
        type="button"
        className="flex items-center justify-between w-full text-left gap-2"
        onClick={onToggleExpand}
        aria-expanded={isExpanded}
      >
        <span className="text-sm font-medium truncate">{displayName(tool.name)}</span>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="text-[11px] text-muted-foreground">
            {subTools.length}
          </span>
          <ChevronIcon expanded={isExpanded} />
        </div>
      </button>

      {isExpanded && (
        <div className="mt-2 pt-2 border-t border-border space-y-0.5">
          {subTools.map((st) => (
            <SubToolRow
              key={st.name}
              subTool={st}
              onPermissionChange={onPermissionChange}
              isUpdating={isUpdating}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SubToolRow({
  subTool,
  onPermissionChange,
  isUpdating,
}: {
  subTool: SubToolEntryResponse;
  onPermissionChange: (toolName: string, level: string) => void;
  isUpdating: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2 py-0.5">
      <span className="text-xs min-w-0 truncate flex items-center gap-1">
        {subToolDisplayName(subTool.name)}
        {subTool.description && (
          <Tooltip content={subTool.description} delay={200} closeDelay={0}>
            <span className="inline-flex text-muted-foreground cursor-help shrink-0">
              <InfoIcon />
            </span>
          </Tooltip>
        )}
      </span>
      <PermissionSelector
        toolName={subToolDisplayName(subTool.name)}
        level={subTool.permission_level as PermLevel}
        onChange={(level) => onPermissionChange(subTool.name, level)}
        disabled={isUpdating}
      />
    </div>
  );
}

function PermissionSelector({
  toolName,
  level,
  onChange,
  disabled,
}: {
  toolName: string;
  level: PermLevel;
  onChange: (level: PermLevel) => void;
  disabled: boolean;
}) {
  return (
    <div className="inline-flex rounded-md border border-border overflow-hidden shrink-0" role="radiogroup" aria-label={`Permission for ${toolName}`}>
      {PERM_OPTIONS.map((opt, i) => {
        const isActive = level === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={isActive}
            disabled={disabled}
            onClick={() => {
              if (!isActive) onChange(opt.value);
            }}
            className={[
              'px-1.5 py-0.5 text-[10px] transition-colors',
              i < PERM_OPTIONS.length - 1 ? 'border-r border-border' : '',
              isActive ? PERM_ACTIVE_STYLES[opt.value] : 'text-muted-foreground hover:bg-muted',
              disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
            ].join(' ')}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function InfoIcon() {
  return (
    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <circle cx="12" cy="12" r="10" />
      <path strokeLinecap="round" d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`w-3.5 h-3.5 text-muted-foreground transition-transform ${expanded ? 'rotate-90' : ''}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </svg>
  );
}

