/**
 * Three-state permission selector for a single sub-tool.
 *
 * The radio surfaces the three values the backend stores: ``always``
 * (auto-run), ``ask`` (prompt before running), and ``never`` (filter
 * the tool out of the LLM schema entirely). Shared by Settings ->
 * Tools and Settings -> Permissions so the user sees the same control
 * for the same underlying preference.
 */

export type PermLevel = 'always' | 'ask' | 'never';

export const PERM_OPTIONS: { value: PermLevel; label: string }[] = [
  { value: 'always', label: 'Auto' },
  { value: 'ask', label: 'Ask first' },
  { value: 'never', label: 'Off' },
];

export const PERM_ACTIVE_STYLES: Record<PermLevel, string> = {
  always: 'bg-muted text-success font-medium',
  ask: 'bg-muted text-warning font-medium',
  never: 'bg-muted text-danger font-medium',
};

export default function PermissionSelector({
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
    <div
      className="inline-flex rounded-md border border-border overflow-hidden shrink-0"
      role="radiogroup"
      aria-label={`Permission for ${toolName}`}
    >
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
