// Shared pagination control for admin list views.
//
// Three near-identical copies previously lived inline in users.tsx,
// reported.tsx, and shared.tsx (as `SimplePagination`). This is a
// straight refactor; behavior matches the previous "Prev / Page X of Y
// / Next" layout used in the shared and reported tabs, plus the
// optional "go to page" form used in the users tab.

interface PaginationProps {
  page: number;
  total: number;
  pageSize: number;
  onChange: (page: number) => void;
  /** When true, render a small "Go to" form once total pages > 3. Used by the Users tab. */
  showGoTo?: boolean;
  /** Optional total-count label shown next to the controls (e.g. "120 users"). */
  countLabel?: string;
}

export default function Pagination({
  page,
  total,
  pageSize,
  onChange,
  showGoTo = false,
  countLabel,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mt-4 text-xs text-muted-foreground">
      {countLabel && <span>{countLabel}</span>}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          className="px-3 py-1 rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-30"
          disabled={page === 0}
          onClick={() => onChange(page - 1)}
        >
          Prev
        </button>
        <span>
          Page {page + 1} of {totalPages}
        </span>
        <button
          type="button"
          className="px-3 py-1 rounded-[--radius-sm] border border-border hover:bg-secondary-hover disabled:opacity-30"
          disabled={page + 1 >= totalPages}
          onClick={() => onChange(page + 1)}
        >
          Next
        </button>
        {showGoTo && totalPages > 3 && (
          <GoToPageForm totalPages={totalPages} onJump={onChange} />
        )}
      </div>
    </div>
  );
}

function GoToPageForm({
  totalPages,
  onJump,
}: {
  totalPages: number;
  onJump: (page: number) => void;
}) {
  return (
    <form
      className="flex items-center gap-1"
      onSubmit={e => {
        e.preventDefault();
        const input = e.currentTarget.elements.namedItem('page') as HTMLInputElement | null;
        if (!input) return;
        const n = Number(input.value);
        if (Number.isFinite(n) && n >= 1 && n <= totalPages) onJump(n - 1);
        input.value = '';
      }}
    >
      <input
        name="page"
        type="text"
        inputMode="numeric"
        placeholder="Go to"
        aria-label={`Go to page (1 to ${totalPages})`}
        className="w-16 px-2 py-1 text-xs bg-card border border-border rounded-[--radius-sm]"
      />
      <button
        type="submit"
        className="px-2 py-1 text-xs rounded-[--radius-sm] border border-border hover:bg-secondary-hover"
      >
        Go
      </button>
    </form>
  );
}
