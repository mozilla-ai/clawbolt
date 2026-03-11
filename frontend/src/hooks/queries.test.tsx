import { renderHook, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createTestQueryClient } from '@/test/test-utils';
import {
  useProfile,
  useMemory,
  useToolConfig,
  useChannelConfig,
  useSessions,
  useSession,
} from './queries';
import api from '@/api';
import type { ReactNode } from 'react';

vi.mock('@/api', () => ({
  default: {
    getProfile: vi.fn(),
    updateProfile: vi.fn(),
    listSessions: vi.fn(),
    getSession: vi.fn(),
    getMemory: vi.fn(),
    updateMemory: vi.fn(),
    getToolConfig: vi.fn(),
    updateToolConfig: vi.fn(),
    getChannelConfig: vi.fn(),
    updateChannelConfig: vi.fn(),
  },
}));

afterEach(() => {
  vi.clearAllMocks();
});

function createWrapper() {
  const queryClient = createTestQueryClient();
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
  };
}

describe('useProfile', () => {
  it('fetches and returns profile data', async () => {
    const mockProfile = { user_text: 'hello', soul_text: 'soul' };
    vi.mocked(api.getProfile).mockResolvedValue(mockProfile as never);

    const { result } = renderHook(() => useProfile(), { wrapper: createWrapper() });

    expect(result.current.isPending).toBe(true);

    await waitFor(() => expect(result.current.isPending).toBe(false));

    expect(result.current.data).toEqual(mockProfile);
    expect(api.getProfile).toHaveBeenCalledOnce();
  });

  it('exposes error when fetch fails', async () => {
    vi.mocked(api.getProfile).mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useProfile(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe('Network error');
  });
});

describe('useMemory', () => {
  it('fetches and returns memory content', async () => {
    const mockMemory = { content: '## Pricing\n- Deck: $45/sqft' };
    vi.mocked(api.getMemory).mockResolvedValue(mockMemory as never);

    const { result } = renderHook(() => useMemory(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(mockMemory);
  });
});

describe('useToolConfig', () => {
  it('fetches and returns tool config', async () => {
    const mockTools = { tools: [{ name: 'estimates', enabled: true, category: 'domain' }] };
    vi.mocked(api.getToolConfig).mockResolvedValue(mockTools as never);

    const { result } = renderHook(() => useToolConfig(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(mockTools);
  });
});

describe('useChannelConfig', () => {
  it('fetches and returns channel config', async () => {
    const mockConfig = { telegram_bot_token_set: true, telegram_allowed_usernames: '*' };
    vi.mocked(api.getChannelConfig).mockResolvedValue(mockConfig as never);

    const { result } = renderHook(() => useChannelConfig(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(mockConfig);
  });
});

describe('useSessions', () => {
  it('fetches and returns paginated sessions', async () => {
    const mockResponse = { sessions: [{ id: 's1' }], total: 1, offset: 0 };
    vi.mocked(api.listSessions).mockResolvedValue(mockResponse as never);

    const { result } = renderHook(() => useSessions(0, 20), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(mockResponse);
    expect(api.listSessions).toHaveBeenCalledWith(0, 20);
  });
});

describe('useSession', () => {
  it('fetches session detail when id is provided', async () => {
    const mockDetail = { session_id: 's1', messages: [] };
    vi.mocked(api.getSession).mockResolvedValue(mockDetail as never);

    const { result } = renderHook(() => useSession('s1'), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(mockDetail);
    expect(api.getSession).toHaveBeenCalledWith('s1');
  });

  it('does not fetch when id is null', async () => {
    const { result } = renderHook(() => useSession(null), { wrapper: createWrapper() });

    // Should remain in pending state but never fire the request
    expect(result.current.fetchStatus).toBe('idle');
    expect(api.getSession).not.toHaveBeenCalled();
  });
});
