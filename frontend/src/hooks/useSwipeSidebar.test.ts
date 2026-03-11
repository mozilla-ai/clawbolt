import { renderHook } from '@testing-library/react';
import useSwipeSidebar from '@/hooks/useSwipeSidebar';

/** Fire a touchstart followed by a touchend to simulate a swipe. */
function simulateSwipe(startX: number, startY: number, endX: number, endY: number) {
  const touchStartEvent = new TouchEvent('touchstart', {
    touches: [{ clientX: startX, clientY: startY } as Touch],
    bubbles: true,
  });
  const touchEndEvent = new TouchEvent('touchend', {
    changedTouches: [{ clientX: endX, clientY: endY } as Touch],
    bubbles: true,
  });
  document.dispatchEvent(touchStartEvent);
  document.dispatchEvent(touchEndEvent);
}

describe('useSwipeSidebar', () => {
  let originalInnerWidth: number;

  beforeEach(() => {
    originalInnerWidth = window.innerWidth;
    // Simulate mobile viewport (< 768)
    Object.defineProperty(window, 'innerWidth', { value: 400, writable: true, configurable: true });
  });

  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', {
      value: originalInnerWidth,
      writable: true,
      configurable: true,
    });
  });

  it('calls onOpen when swiping right from the left edge while closed', () => {
    const onOpen = vi.fn();
    const onClose = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: false, onOpen, onClose }));

    // Swipe starting at x=10 (inside 30px edge zone), moving right 80px
    simulateSwipe(10, 200, 90, 200);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('calls onClose when swiping left while sidebar is open', () => {
    const onOpen = vi.fn();
    const onClose = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: true, onOpen, onClose }));

    // Swipe from x=200 to x=100 (leftward, -100px)
    simulateSwipe(200, 300, 100, 300);

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('ignores swipe-right that does not start in the edge zone', () => {
    const onOpen = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: false, onOpen, onClose: vi.fn() }));

    // Start at x=100, outside the 30px edge zone
    simulateSwipe(100, 200, 200, 200);

    expect(onOpen).not.toHaveBeenCalled();
  });

  it('ignores swipes below the distance threshold', () => {
    const onOpen = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: false, onOpen, onClose: vi.fn() }));

    // Swipe only 30px (below 50px threshold)
    simulateSwipe(10, 200, 40, 200);

    expect(onOpen).not.toHaveBeenCalled();
  });

  it('ignores swipes with too much vertical drift', () => {
    const onOpen = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: false, onOpen, onClose: vi.fn() }));

    // Swipe right 80px but with 100px vertical drift (over 75px limit)
    simulateSwipe(10, 200, 90, 300);

    expect(onOpen).not.toHaveBeenCalled();
  });

  it('does nothing on desktop-width viewports', () => {
    Object.defineProperty(window, 'innerWidth', { value: 1024, writable: true, configurable: true });

    const onOpen = vi.fn();
    const onClose = vi.fn();

    renderHook(() => useSwipeSidebar({ isOpen: false, onOpen, onClose }));

    simulateSwipe(10, 200, 90, 200);

    expect(onOpen).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('cleans up event listeners on unmount', () => {
    const addSpy = vi.spyOn(document, 'addEventListener');
    const removeSpy = vi.spyOn(document, 'removeEventListener');

    const { unmount } = renderHook(() =>
      useSwipeSidebar({ isOpen: false, onOpen: vi.fn(), onClose: vi.fn() }),
    );

    expect(addSpy).toHaveBeenCalledWith('touchstart', expect.any(Function), { passive: true });
    expect(addSpy).toHaveBeenCalledWith('touchend', expect.any(Function), { passive: true });

    unmount();

    expect(removeSpy).toHaveBeenCalledWith('touchstart', expect.any(Function));
    expect(removeSpy).toHaveBeenCalledWith('touchend', expect.any(Function));

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
