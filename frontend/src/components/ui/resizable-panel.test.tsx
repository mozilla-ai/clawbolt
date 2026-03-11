import { render, screen, fireEvent, act } from '@testing-library/react';
import ResizablePanel from './resizable-panel';

// Stub requestAnimationFrame to execute callbacks synchronously in tests.
beforeEach(() => {
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => {
    cb(0);
    return 0;
  });
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ResizablePanel', () => {
  it('renders with default width', () => {
    render(
      <ResizablePanel side="right" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );
    const panel = screen.getByTestId('resizable-panel');
    expect(panel).toBeInTheDocument();
    expect(panel.style.width).toBe('300px');
  });

  it('renders children', () => {
    render(
      <ResizablePanel side="left" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>Hello panel</p>
      </ResizablePanel>,
    );
    expect(screen.getByText('Hello panel')).toBeInTheDocument();
  });

  it('respects localStorage persisted width', () => {
    localStorage.setItem('test-key', '400');
    render(
      <ResizablePanel
        side="right"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        storageKey="test-key"
      >
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').style.width).toBe('400px');
  });

  it('falls back to defaultWidth when localStorage has invalid value', () => {
    localStorage.setItem('test-key', 'garbage');
    render(
      <ResizablePanel
        side="right"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        storageKey="test-key"
      >
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').style.width).toBe('300px');
  });

  it('clamps persisted width to min bound', () => {
    localStorage.setItem('test-key', '100');
    render(
      <ResizablePanel
        side="right"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        storageKey="test-key"
      >
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').style.width).toBe('200px');
  });

  it('clamps persisted width to max bound', () => {
    localStorage.setItem('test-key', '900');
    render(
      <ResizablePanel
        side="right"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        storageKey="test-key"
      >
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').style.width).toBe('500px');
  });

  it('clamps defaultWidth to min/max bounds', () => {
    render(
      <ResizablePanel side="right" defaultWidth={100} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').style.width).toBe('200px');
  });

  it('drag interaction updates width (side="left")', () => {
    render(
      <ResizablePanel side="left" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );

    const handle = screen.getByTestId('resizable-handle');
    const panel = screen.getByTestId('resizable-panel');

    // Start drag at x=300, move to x=350 -> delta +50 -> 350px for side="left"
    act(() => {
      fireEvent.mouseDown(handle, { clientX: 300 });
    });
    act(() => {
      fireEvent.mouseMove(document, { clientX: 350 });
    });
    act(() => {
      fireEvent.mouseUp(document);
    });

    expect(panel.style.width).toBe('350px');
  });

  it('drag interaction updates width (side="right")', () => {
    render(
      <ResizablePanel side="right" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );

    const handle = screen.getByTestId('resizable-handle');
    const panel = screen.getByTestId('resizable-panel');

    // For side="right", moving left (decreasing clientX) increases width.
    // Start at x=100, move to x=50 -> delta = 100-50 = 50 -> 350px
    act(() => {
      fireEvent.mouseDown(handle, { clientX: 100 });
    });
    act(() => {
      fireEvent.mouseMove(document, { clientX: 50 });
    });
    act(() => {
      fireEvent.mouseUp(document);
    });

    expect(panel.style.width).toBe('350px');
  });

  it('clamps drag result to min/max', () => {
    render(
      <ResizablePanel side="left" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );

    const handle = screen.getByTestId('resizable-handle');
    const panel = screen.getByTestId('resizable-panel');

    // Drag far right: 300 + 500 = 800 -> clamped to 500
    act(() => {
      fireEvent.mouseDown(handle, { clientX: 0 });
    });
    act(() => {
      fireEvent.mouseMove(document, { clientX: 500 });
    });
    act(() => {
      fireEvent.mouseUp(document);
    });

    expect(panel.style.width).toBe('500px');
  });

  it('persists final width to localStorage after drag', () => {
    render(
      <ResizablePanel
        side="left"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        storageKey="persist-test"
      >
        <p>content</p>
      </ResizablePanel>,
    );

    const handle = screen.getByTestId('resizable-handle');

    act(() => {
      fireEvent.mouseDown(handle, { clientX: 0 });
    });
    act(() => {
      fireEvent.mouseMove(document, { clientX: 50 });
    });
    act(() => {
      fireEvent.mouseUp(document);
    });

    expect(localStorage.getItem('persist-test')).toBe('350');
  });

  it('renders handle with separator role', () => {
    render(
      <ResizablePanel side="right" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );

    const handle = screen.getByRole('separator');
    expect(handle).toBeInTheDocument();
    expect(handle.getAttribute('aria-orientation')).toBe('vertical');
  });

  it('applies custom className', () => {
    render(
      <ResizablePanel
        side="left"
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
        className="custom-class"
      >
        <p>content</p>
      </ResizablePanel>,
    );
    expect(screen.getByTestId('resizable-panel').className).toContain('custom-class');
  });

  it('disables CSS transition during drag', () => {
    render(
      <ResizablePanel side="left" defaultWidth={300} minWidth={200} maxWidth={500}>
        <p>content</p>
      </ResizablePanel>,
    );

    const panel = screen.getByTestId('resizable-panel');
    const handle = screen.getByTestId('resizable-handle');

    // Before drag: transition is set
    expect(panel.style.transition).toContain('width');

    // During drag: transition is none
    act(() => {
      fireEvent.mouseDown(handle, { clientX: 0 });
    });
    expect(panel.style.transition).toBe('none');

    // After drag: transition is restored
    act(() => {
      fireEvent.mouseUp(document);
    });
    expect(panel.style.transition).toContain('width');
  });
});
