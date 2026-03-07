import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement, ReactNode } from 'react';

interface RouterRenderOptions extends Omit<RenderOptions, 'wrapper'> {
  route?: string;
}

/**
 * Render helper that wraps components in MemoryRouter.
 * Use this for any component that uses React Router hooks.
 */
export function renderWithRouter(ui: ReactElement, { route = '/', ...options }: RouterRenderOptions = {}) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[route]}>
        {children}
      </MemoryRouter>
    );
  }
  return render(ui, { wrapper: Wrapper, ...options });
}
