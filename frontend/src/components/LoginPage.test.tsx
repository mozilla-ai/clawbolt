import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import LoginPage from './LoginPage';

const mockNavigate = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authState: 'unauthenticated', handleLogin: vi.fn() }),
}));

describe('LoginPage', () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    window.history.replaceState(null, '', '/login');
  });

  it('renders the Google sign-in button', () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );
    expect(screen.getByText('Sign in with Google')).toBeInTheDocument();
  });
});
