import { useEffect, useRef, useCallback, type ReactNode } from 'react';
import Button from '@/components/ui/button';

interface ConfirmModalProps {
  isOpen: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  variant?: 'danger' | 'primary';
  isLoading?: boolean;
}

export default function ConfirmModal({
  isOpen,
  onConfirm,
  onCancel,
  title,
  message,
  confirmLabel = 'Confirm',
  variant = 'danger',
  isLoading = false,
}: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isLoading) onCancel();
    },
    [onCancel, isLoading],
  );

  useEffect(() => {
    if (!isOpen) return;
    document.addEventListener('keydown', handleKeyDown);
    cancelRef.current?.focus();
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, handleKeyDown]);

  if (!isOpen) return null;

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === overlayRef.current && !isLoading) onCancel();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-modal-title"
    >
      <div className="bg-card border border-border rounded-lg shadow-lg w-full max-w-sm mx-4 p-5 animate-message-in">
        <h3
          id="confirm-modal-title"
          className="text-base font-semibold font-display text-foreground"
        >
          {title}
        </h3>
        <div className="mt-2 text-sm text-muted-foreground">{message}</div>
        <div className="mt-5 flex justify-end gap-2">
          <Button
            ref={cancelRef}
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={isLoading}
          >
            Cancel
          </Button>
          <Button
            variant={variant}
            size="sm"
            onClick={onConfirm}
            disabled={isLoading}
            isLoading={isLoading}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
