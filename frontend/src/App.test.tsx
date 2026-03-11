import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '@/contexts/AuthContext';
import App from '@/App';

// Mock fetch for auth config (OSS mode: not required) and subsequent API calls.
// Each call returns a fresh Response so the body is never re-consumed.
beforeEach(() => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ required: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('App', () => {
  it('renders loading spinner initially', () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('lazy-loads ChatPage on /app/chat route', async () => {
    render(
      <MemoryRouter initialEntries={['/app/chat']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });
  });

  it('lazy-loads MemoryPage on /app/memory route', async () => {
    render(
      <MemoryRouter initialEntries={['/app/memory']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Memory / Facts')).toBeInTheDocument();
    });
  });

  it('lazy-loads ToolsPage on /app/tools route', async () => {
    render(
      <MemoryRouter initialEntries={['/app/tools']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Tools')).toBeInTheDocument();
    });
  });

  it('lazy-loads ConversationsPage on /app/conversations route', async () => {
    render(
      <MemoryRouter initialEntries={['/app/conversations']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });
  });
});
