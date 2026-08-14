import { useEffect, useRef, type ReactNode } from 'react';

interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
}

export default function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  busy = false,
}: ConfirmDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  // Hold the latest busy/onClose in refs so the focus effect can depend
  // only on `open`. Callers typically pass fresh arrow functions each
  // render (e.g. `onClose={() => setOpen(false)}`); if those were in the
  // dep array, every parent re-render (such as on each keystroke into an
  // input rendered inside `description`) would tear down the effect,
  // refocusing the previously-focused element and pulling focus out of
  // the input the user is typing into.
  const busyRef = useRef(busy);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    busyRef.current = busy;
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    // Capture the element that had focus when the dialog opened so we can
    // restore focus to it on close (a11y: keyboard users shouldn't lose place).
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busyRef.current) onCloseRef.current();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      // Restore focus when the dialog closes (component unmounts or `open`
      // flips false). Guard against the previously focused node being
      // removed from the DOM in the meantime.
      const prev = previouslyFocusedRef.current;
      if (prev && document.contains(prev)) {
        prev.focus();
      }
      previouslyFocusedRef.current = null;
    };
  }, [open]);

  if (!open) return null;

  const confirmClass = destructive
    ? 'bg-danger text-danger-foreground hover:opacity-90'
    : 'bg-primary text-primary-foreground hover:bg-primary-hover';

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      <div
        className="absolute inset-0 bg-foreground/30 backdrop-blur-sm"
        onClick={busy ? undefined : onClose}
      />
      <div className="relative bg-card border border-border rounded-[--radius-lg] shadow-lg max-w-md w-full p-5 animate-[dialog-in_150ms_ease-out]">
        <h3 id="confirm-dialog-title" className="text-base font-semibold mb-2">
          {title}
        </h3>
        <div className="text-sm text-muted-foreground mb-5">{description}</div>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="px-3 py-2 text-sm font-medium rounded-[--radius-md] border border-border hover:bg-secondary-hover disabled:opacity-50"
            onClick={onClose}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`px-3 py-2 text-sm font-medium rounded-[--radius-md] disabled:opacity-50 ${confirmClass}`}
            onClick={() => void onConfirm()}
            disabled={busy}
          >
            {busy ? 'Working...' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
