import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement, ReactNode } from 'react';

interface RouterRenderOptions extends Omit<RenderOptions, 'wrapper'> {
  route?: string;
}

/** Create a fresh QueryClient configured for tests (no retries, no refetch). */
export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

/**
 * Render helper that wraps components in MemoryRouter and QueryClientProvider.
 * Use this for any component that uses React Router hooks or React Query.
 */
export function renderWithRouter(ui: ReactElement, { route = '/', ...options }: RouterRenderOptions = {}) {
  const queryClient = createTestQueryClient();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    );
  }
  return render(ui, { wrapper: Wrapper, ...options });
}
