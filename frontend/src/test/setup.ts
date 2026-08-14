import '@testing-library/jest-dom';

// jsdom doesn't implement ResizeObserver; stub it for components that watch layout.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver = ResizeObserverStub;
}

// jsdom doesn't implement the Blob object URL helpers; stub them so file
// downloads (e.g. the admin share-snippet PNG) can be exercised in tests.
if (typeof URL.createObjectURL === 'undefined') {
  URL.createObjectURL = () => 'blob:stub';
}
if (typeof URL.revokeObjectURL === 'undefined') {
  URL.revokeObjectURL = () => {};
}
