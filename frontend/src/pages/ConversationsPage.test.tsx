import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ConversationsPage from './ConversationsPage';
import type { SessionListResponse, SessionSummary } from '@/types';

// Mock the api module
vi.mock('@/api', () => ({
  default: {
    listSessions: vi.fn(),
    getSession: vi.fn(),
  },
}));

import api from '@/api';
const mockApi = vi.mocked(api);

// --- IntersectionObserver mock ---

type ObserverCallback = (entries: IntersectionObserverEntry[]) => void;
let observerCallback: ObserverCallback | null = null;

class MockIntersectionObserver {
  constructor(callback: ObserverCallback) {
    observerCallback = callback;
  }
  observe(_el: Element) { /* no-op */ }
  unobserve() { /* no-op */ }
  disconnect() {
    observerCallback = null;
  }
}

beforeAll(() => {
  vi.stubGlobal('IntersectionObserver', MockIntersectionObserver);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

// --- Helpers ---

function makeSession(id: string): SessionSummary {
  return {
    id,
    start_time: '2025-01-01T00:00:00Z',
    message_count: 3,
    last_message_preview: `Preview ${id}`,
    channel: 'webchat',
  };
}

function makeResponse(
  sessions: SessionSummary[],
  total: number,
  offset: number,
): SessionListResponse {
  return { sessions, total, offset, limit: 20 };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/app/conversations']}>
      <ConversationsPage />
    </MemoryRouter>,
  );
}

// --- Tests ---

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
  observerCallback = null;
});

describe('ConversationsPage - infinite scroll', () => {
  it('renders initial sessions and shows total count', async () => {
    const sessions = Array.from({ length: 5 }, (_, i) => makeSession(`s${i}`));
    mockApi.listSessions.mockResolvedValueOnce(makeResponse(sessions, 5, 0));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('5 conversations')).toBeInTheDocument();
    });
    expect(screen.getByText('Preview s0')).toBeInTheDocument();
    expect(screen.getByText('Preview s4')).toBeInTheDocument();
  });

  it('shows empty state when no sessions exist', async () => {
    mockApi.listSessions.mockResolvedValueOnce(makeResponse([], 0, 0));

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText(/No conversations yet/),
      ).toBeInTheDocument();
    });
  });

  it('loads more sessions when sentinel becomes visible', async () => {
    const page1 = Array.from({ length: 20 }, (_, i) => makeSession(`s${i}`));
    const page2 = Array.from({ length: 5 }, (_, i) => makeSession(`s${i + 20}`));

    mockApi.listSessions
      .mockResolvedValueOnce(makeResponse(page1, 25, 0))
      .mockResolvedValueOnce(makeResponse(page2, 25, 20));

    renderPage();

    // Wait for initial load
    await waitFor(() => {
      expect(screen.getByText('25 conversations')).toBeInTheDocument();
    });
    expect(screen.getByText('Preview s0')).toBeInTheDocument();

    // Verify the sentinel is in the DOM
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();

    // Simulate intersection (sentinel becomes visible)
    await act(async () => {
      observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry]);
    });

    // Wait for second page to load
    await waitFor(() => {
      expect(screen.getByText('Preview s20')).toBeInTheDocument();
    });

    // Second fetch should have been called with offset=20
    expect(mockApi.listSessions).toHaveBeenCalledTimes(2);
    expect(mockApi.listSessions).toHaveBeenLastCalledWith(20, 20);
  });

  it('shows "All conversations loaded" when all pages fetched', async () => {
    // More than PAGE_SIZE (20) sessions total, but all loaded after two pages
    const page1 = Array.from({ length: 20 }, (_, i) => makeSession(`s${i}`));
    const page2 = Array.from({ length: 5 }, (_, i) => makeSession(`s${i + 20}`));

    mockApi.listSessions
      .mockResolvedValueOnce(makeResponse(page1, 25, 0))
      .mockResolvedValueOnce(makeResponse(page2, 25, 20));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('25 conversations')).toBeInTheDocument();
    });

    // Trigger intersection
    await act(async () => {
      observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry]);
    });

    await waitFor(() => {
      expect(screen.getByText('All conversations loaded')).toBeInTheDocument();
    });
  });

  it('does not show pagination buttons', async () => {
    const sessions = Array.from({ length: 20 }, (_, i) => makeSession(`s${i}`));
    mockApi.listSessions.mockResolvedValueOnce(makeResponse(sessions, 40, 0));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('40 conversations')).toBeInTheDocument();
    });

    expect(screen.queryByText('Previous')).not.toBeInTheDocument();
    expect(screen.queryByText('Next')).not.toBeInTheDocument();
    expect(screen.queryByText(/Page \d+ of \d+/)).not.toBeInTheDocument();
  });

  it('does not fetch more when already at end', async () => {
    const sessions = Array.from({ length: 5 }, (_, i) => makeSession(`s${i}`));
    mockApi.listSessions.mockResolvedValueOnce(makeResponse(sessions, 5, 0));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('5 conversations')).toBeInTheDocument();
    });

    // The observer should not trigger a second fetch since hasMore is false
    // (sessions.length === total)
    expect(mockApi.listSessions).toHaveBeenCalledTimes(1);
  });

  it('shows error state with retry button', async () => {
    mockApi.listSessions.mockRejectedValueOnce(new Error('Server error'));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Server error')).toBeInTheDocument();
    });
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });
});
