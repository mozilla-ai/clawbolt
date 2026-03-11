import {
  useRef,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
  type CSSProperties,
} from 'react';

interface ResizablePanelProps {
  /** Which side of the viewport the panel is on. Determines drag handle placement. */
  side: 'left' | 'right';
  /** Initial width in pixels when no persisted value exists. */
  defaultWidth: number;
  /** Minimum allowed width in pixels. */
  minWidth: number;
  /** Maximum allowed width in pixels. */
  maxWidth: number;
  /** localStorage key for persisting the panel width across sessions. */
  storageKey?: string;
  children: ReactNode;
  className?: string;
}

function readPersistedWidth(key: string | undefined): number | null {
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

function persistWidth(key: string | undefined, width: number): void {
  if (!key) return;
  try {
    localStorage.setItem(key, String(width));
  } catch {
    /* storage full or unavailable */
  }
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export default function ResizablePanel({
  side,
  defaultWidth,
  minWidth,
  maxWidth,
  storageKey,
  children,
  className,
}: ResizablePanelProps) {
  const [width, setWidth] = useState(() => {
    const persisted = readPersistedWidth(storageKey);
    return clamp(persisted ?? defaultWidth, minWidth, maxWidth);
  });
  const [dragging, setDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const dragStartX = useRef(0);
  const dragStartWidth = useRef(0);

  // Persist width whenever it changes (and we are not mid-drag to avoid thrashing).
  const pendingPersist = useRef<number | null>(null);
  useEffect(() => {
    if (dragging) return;
    persistWidth(storageKey, width);
  }, [width, dragging, storageKey]);

  const handleDrag = useCallback(
    (clientX: number) => {
      const delta =
        side === 'left'
          ? clientX - dragStartX.current
          : dragStartX.current - clientX;
      const newWidth = clamp(dragStartWidth.current + delta, minWidth, maxWidth);
      if (pendingPersist.current !== null) {
        cancelAnimationFrame(pendingPersist.current);
      }
      pendingPersist.current = requestAnimationFrame(() => {
        setWidth(newWidth);
      });
    },
    [side, minWidth, maxWidth],
  );

  const stopDrag = useCallback(() => {
    setDragging(false);
  }, []);

  // Global mouse/touch move and up listeners while dragging.
  useEffect(() => {
    if (!dragging) return;

    const onMouseMove = (e: MouseEvent) => {
      e.preventDefault();
      handleDrag(e.clientX);
    };
    const onTouchMove = (e: TouchEvent) => {
      const touch = e.touches[0];
      if (touch) handleDrag(touch.clientX);
    };
    const onEnd = () => stopDrag();

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onTouchMove, { passive: true });
    document.addEventListener('touchend', onEnd);
    document.addEventListener('touchcancel', onEnd);

    // Prevent text selection while dragging.
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';

    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onEnd);
      document.removeEventListener('touchmove', onTouchMove);
      document.removeEventListener('touchend', onEnd);
      document.removeEventListener('touchcancel', onEnd);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
  }, [dragging, handleDrag, stopDrag]);

  const startDrag = (clientX: number) => {
    dragStartX.current = clientX;
    dragStartWidth.current = width;
    setDragging(true);
  };

  const onMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    startDrag(e.clientX);
  };

  const onTouchStart = (e: React.TouchEvent) => {
    e.preventDefault();
    const touch = e.touches[0];
    if (touch) startDrag(touch.clientX);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const step = 10;
    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      setWidth((w) => clamp(w - step, minWidth, maxWidth));
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      setWidth((w) => clamp(w + step, minWidth, maxWidth));
    }
  };

  // Cancel pending requestAnimationFrame on unmount
  useEffect(() => {
    return () => {
      if (pendingPersist.current !== null) {
        cancelAnimationFrame(pendingPersist.current);
      }
    };
  }, []);

  const panelStyle: CSSProperties = {
    width: `${width}px`,
    flexShrink: 0,
    transition: dragging ? 'none' : 'width 0.15s ease',
  };

  const handleStyle: CSSProperties = {
    position: 'absolute',
    top: 0,
    bottom: 0,
    width: '4px',
    cursor: 'col-resize',
    zIndex: 10,
    ...(side === 'left' ? { right: 0 } : { left: 0 }),
  };

  return (
    <div
      ref={panelRef}
      data-testid="resizable-panel"
      className={className}
      style={{ ...panelStyle, position: 'relative', overflow: 'hidden' }}
    >
      {children}
      <div
        data-testid="resizable-handle"
        role="separator"
        aria-orientation="vertical"
        aria-valuenow={width}
        aria-valuemin={minWidth}
        aria-valuemax={maxWidth}
        tabIndex={0}
        aria-label="Resize panel"
        style={handleStyle}
        onMouseDown={onMouseDown}
        onTouchStart={onTouchStart}
        onKeyDown={onKeyDown}
      >
        <div
          style={{
            width: '100%',
            height: '100%',
            backgroundColor: dragging
              ? 'var(--color-primary, #2563eb)'
              : 'transparent',
            transition: 'background-color 0.15s ease',
          }}
          onMouseEnter={(e) => {
            if (!dragging)
              (e.currentTarget.style.backgroundColor =
                'var(--color-border, #e5e7eb)');
          }}
          onMouseLeave={(e) => {
            if (!dragging) (e.currentTarget.style.backgroundColor = 'transparent');
          }}
        />
      </div>
    </div>
  );
}
