import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HomePage from './HomePage';

const mockNavigate = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

let mockAuthState = 'unauthenticated';

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authState: mockAuthState }),
}));

describe('HomePage', () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    mockAuthState = 'unauthenticated';
  });

  it('shows the hero page when not logged in', () => {
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    );
    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('redirects to /app when already authenticated', () => {
    mockAuthState = 'ready';
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    );
    expect(mockNavigate).toHaveBeenCalledWith('/app', { replace: true });
  });

  it('submits the waitlist form with both name and email', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }));

    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('Your name'), { target: { value: 'Alice' } });
    fireEvent.change(screen.getByPlaceholderText(/What trade are you in/i), {
      target: { value: 'Residential plumbing in Phoenix' },
    });
    fireEvent.change(screen.getByPlaceholderText('Email address'), {
      target: { value: 'alice@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Request Early Access/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const init = fetchMock.mock.calls[0]?.[1];
    expect(init).toBeDefined();
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: 'alice@example.com',
      name: 'Alice',
      use_case: 'Residential plumbing in Phoenix',
      source: 'homepage',
    });

    fetchMock.mockRestore();
  });

  it('submits with empty use_case when the textarea is left blank', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }));

    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('Your name'), { target: { value: 'Bob' } });
    fireEvent.change(screen.getByPlaceholderText('Email address'), {
      target: { value: 'bob@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Request Early Access/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const init = fetchMock.mock.calls[0]?.[1];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: 'bob@example.com',
      name: 'Bob',
      use_case: '',
      source: 'homepage',
    });

    fetchMock.mockRestore();
  });
});
