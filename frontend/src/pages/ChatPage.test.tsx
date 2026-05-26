import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import { ChatActivityProvider } from '@/contexts/ChatActivityContext';
import ChatPage from './ChatPage';

// Mock the api module
vi.mock('@/api', () => ({
  default: {
    getConversation: vi.fn(),
    getConversationSystemPrompt: vi.fn(),
    sendChatMessage: vi.fn(),
    deleteConversationHistory: vi.fn().mockResolvedValue(undefined),
    deleteMessage: vi.fn().mockResolvedValue(undefined),
    deleteMessages: vi.fn().mockResolvedValue(undefined),
    subscribeToActivity: vi.fn().mockReturnValue(new AbortController()),
  },
}));

// Re-import after mock so we can control return values
import api from '@/api';
const mockApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('ChatPage auto-focus', () => {
  it('focuses the chat input on mount', async () => {
    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>);

    const textarea = screen.getByPlaceholderText('Type a message...');
    await waitFor(() => {
      expect(document.activeElement).toBe(textarea);
    });
  });
});

describe('ChatPage tool interactions', () => {
  it('displays tool interactions when loading session history', async () => {
    const sessionId = '1_1000';
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Create an estimate',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'I created the estimate for you.',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [
            { name: 'create_estimate', result: 'Estimate created successfully' },
            { name: 'send_message', result: 'Message sent' },
          ],
        },
      ],
    });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    await waitFor(() => {
      expect(screen.getByText('I created the estimate for you.')).toBeInTheDocument();
    });

    // Tool interactions should be visible
    expect(screen.getByText('create_estimate')).toBeInTheDocument();
    expect(screen.getByText('send_message')).toBeInTheDocument();
  });

  it('does not render tool section when there are no tool interactions', async () => {
    const sessionId = '1_2000';
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Hello',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'Hi there!',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [],
        },
      ],
    });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    await waitFor(() => {
      expect(screen.getByText('Hi there!')).toBeInTheDocument();
    });

    // No "Tool:" labels should appear
    expect(screen.queryByText('Tool:')).not.toBeInTheDocument();
  });
});

describe('ChatPage tool interaction expand/collapse', () => {
  const sessionId = '1_4000';

  function mockSessionWithTools(toolInteractions: Record<string, unknown>[]) {
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Do something',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'Done.',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: toolInteractions,
        },
      ],
    });
  }

  it('expands a tool interaction to show full result on click', async () => {
    const fullResult = 'A'.repeat(200);
    mockSessionWithTools([
      { name: 'long_tool', args: {}, result: fullResult, is_error: false, tool_call_id: 'tc_123' },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('long_tool')).toBeInTheDocument();
    });

    // Should show truncated result by default
    expect(screen.getByText('A'.repeat(80) + '...')).toBeInTheDocument();
    // Full result should not be visible
    expect(screen.queryByText(fullResult)).not.toBeInTheDocument();

    // Click to expand
    await user.click(screen.getByText('long_tool'));

    // Full result should now be visible
    expect(screen.getByText(fullResult)).toBeInTheDocument();
    // tool_call_id should be visible
    expect(screen.getByText('tc_123')).toBeInTheDocument();
  });

  it('collapses an expanded tool interaction on second click', async () => {
    mockSessionWithTools([
      { name: 'toggle_tool', args: {}, result: 'some result', is_error: false, tool_call_id: 'tc_456' },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('toggle_tool')).toBeInTheDocument();
    });

    // Expand
    await user.click(screen.getByText('toggle_tool'));
    expect(screen.getByText('tc_456')).toBeInTheDocument();

    // Collapse
    await user.click(screen.getByText('toggle_tool'));
    expect(screen.queryByText('tc_456')).not.toBeInTheDocument();
  });

  it('shows error badge for tool interactions with is_error true', async () => {
    mockSessionWithTools([
      { name: 'failing_tool', args: {}, result: 'Something went wrong', is_error: true },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    await waitFor(() => {
      expect(screen.getByText('failing_tool')).toBeInTheDocument();
    });

    expect(screen.getByText('Error')).toBeInTheDocument();
  });

  it('shows formatted args when expanded and args are present', async () => {
    mockSessionWithTools([
      {
        name: 'args_tool',
        args: { customer: 'John', amount: 500 },
        result: 'OK',
        is_error: false,
      },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('args_tool')).toBeInTheDocument();
    });

    // Args label should not be visible when collapsed
    expect(screen.queryByText('Args')).not.toBeInTheDocument();

    // Expand
    await user.click(screen.getByText('args_tool'));

    // Args label and formatted JSON should be visible
    expect(screen.getByText('Args')).toBeInTheDocument();
    expect(screen.getByText(/"customer": "John"/)).toBeInTheDocument();
  });

  it('hides args section when args are empty', async () => {
    mockSessionWithTools([
      { name: 'no_args_tool', args: {}, result: 'Done', is_error: false },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('no_args_tool')).toBeInTheDocument();
    });

    // Expand
    await user.click(screen.getByText('no_args_tool'));

    // Args section should not be shown
    expect(screen.queryByText('Args')).not.toBeInTheDocument();
    // Result should still be shown
    expect(screen.getByText('Done')).toBeInTheDocument();
  });

  it('shows "No result" placeholder when result is empty', async () => {
    mockSessionWithTools([
      { name: 'empty_result_tool', args: { key: 'val' }, result: '', is_error: false },
    ]);

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('empty_result_tool')).toBeInTheDocument();
    });

    // Expand
    await user.click(screen.getByText('empty_result_tool'));

    expect(screen.getByText('No result')).toBeInTheDocument();
  });
});

describe('ChatPage message body wrapping', () => {
  it('wraps long unbreakable strings (e.g. URLs) inside the message bubble', async () => {
    const sessionId = '1_3000';
    const longUrl =
      'https://example.com/oauth/connect?state=' + 'a'.repeat(200) + '&redirect=https://app.example.com/callback';
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Connect my calendar',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: longUrl,
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [],
        },
      ],
    });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: `/app/chat?session=${sessionId}` });

    const body = await screen.findByText(longUrl);
    expect(body.className).toContain('break-words');
  });
});

describe('ChatPage conversation auto-load', () => {
  it('loads the user conversation on mount', async () => {
    mockApi.getConversation.mockResolvedValue({
      session_id: 'sess-1',
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Previous message',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'Previous reply',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [],
        },
      ],
    });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: '/app/chat' });

    await waitFor(() => {
      expect(mockApi.getConversation).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(screen.getByText('Previous message')).toBeInTheDocument();
      expect(screen.getByText('Previous reply')).toBeInTheDocument();
    });
  });

  it('shows empty state when no conversation exists yet', async () => {
    mockApi.getConversation.mockResolvedValue({
      session_id: '',
      user_id: '1',
      created_at: '',
      last_message_at: '',
      channel: '',
      initial_system_prompt: '',
      messages: [],
    });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: '/app/chat' });

    await waitFor(() => {
      expect(screen.getByText('Send a message to start chatting.')).toBeInTheDocument();
    });
  });
});

describe('ChatPage concurrent messaging', () => {
  it('keeps input and send button enabled while assistant is responding', async () => {
    // sendChatMessage never resolves, simulating a pending response
    mockApi.sendChatMessage.mockReturnValue(new Promise(() => {}));

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>);

    const textarea = screen.getByPlaceholderText('Type a message...');
    const user = userEvent.setup();

    // Type and send a message
    await user.type(textarea, 'Hello');
    await user.keyboard('{Enter}');

    // Wait for the user message to appear in the chat
    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument();
    });

    // Input should NOT be disabled while the assistant is responding
    expect(textarea).not.toBeDisabled();

    // User should be able to type a new message while waiting
    await user.type(textarea, 'Follow up');
    expect(textarea).toHaveValue('Follow up');

    // Send button should be enabled since there is text in the input
    const sendButton = screen.getByLabelText('Send message');
    expect(sendButton).not.toBeDisabled();

    // Attach files button should also remain enabled
    const attachButton = screen.getByLabelText('Attach files');
    expect(attachButton).not.toBeDisabled();
  });
});

describe('ChatPage current system prompt panel', () => {
  it('lazy-loads the live system prompt only when the user expands the panel', async () => {
    const sessionId = '1_5000';
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: 'STALE FIRST-TURN PROMPT',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'Hi',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'Hello!',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [],
        },
      ],
    });
    mockApi.getConversationSystemPrompt.mockResolvedValue({
      session_id: sessionId,
      system_prompt: 'LIVE PROMPT FROM ENDPOINT',
      is_onboarding: false,
    });

    renderWithRouter(
      <ChatActivityProvider><ChatPage /></ChatActivityProvider>,
      { route: `/app/chat?session=${sessionId}` },
    );

    // Wait for messages to render so we know the session has loaded.
    await waitFor(() => {
      expect(screen.getByText('Hello!')).toBeInTheDocument();
    });

    // Panel toggle is visible but the endpoint hasn't been hit yet.
    const toggle = screen.getByRole('button', { name: /current system prompt/i });
    expect(toggle).toBeInTheDocument();
    expect(mockApi.getConversationSystemPrompt).not.toHaveBeenCalled();

    // We must NOT show the stale frozen snapshot. Even before opening
    // the panel, the old initial_system_prompt text should be absent.
    expect(screen.queryByText('STALE FIRST-TURN PROMPT')).not.toBeInTheDocument();

    // Expanding triggers a single fetch with the active session id and
    // renders the live prompt body.
    const user = userEvent.setup();
    await user.click(toggle);

    await waitFor(() => {
      expect(screen.getByText('LIVE PROMPT FROM ENDPOINT')).toBeInTheDocument();
    });
    expect(mockApi.getConversationSystemPrompt).toHaveBeenCalled();
  });

  it('shows the Onboarding badge when the live prompt is in onboarding mode', async () => {
    const sessionId = '1_5001';
    mockApi.getConversation.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [
        {
          seq: 1,
          direction: 'inbound',
          body: 'hey',
          timestamp: '2025-01-01T00:00:00Z',
          tool_interactions: [],
        },
        {
          seq: 2,
          direction: 'outbound',
          body: 'Hi, I am Clawbolt',
          timestamp: '2025-01-01T00:01:00Z',
          tool_interactions: [],
        },
      ],
    });
    mockApi.getConversationSystemPrompt.mockResolvedValue({
      session_id: sessionId,
      system_prompt: 'BOOTSTRAP CONTENT',
      is_onboarding: true,
    });

    renderWithRouter(
      <ChatActivityProvider><ChatPage /></ChatActivityProvider>,
      { route: `/app/chat?session=${sessionId}` },
    );
    await waitFor(() => {
      expect(screen.getByText('Hi, I am Clawbolt')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /current system prompt/i }));

    await waitFor(() => {
      expect(screen.getByText('BOOTSTRAP CONTENT')).toBeInTheDocument();
    });
    // The Onboarding badge should appear once the live prompt resolves.
    expect(screen.getByText(/onboarding/i)).toBeInTheDocument();
  });
});

describe('ChatPage failed-send cleanup (issue #1368)', () => {
  it('removes the optimistic user message when sendChatMessage rejects', async () => {
    // Empty conversation so the only visible user message is the one we send.
    mockApi.getConversation.mockResolvedValue({
      session_id: '',
      user_id: '1',
      created_at: '',
      last_message_at: '',
      channel: 'webchat',
      initial_system_prompt: '',
      messages: [],
    });
    mockApi.sendChatMessage.mockRejectedValue(new Error('Request failed: 403'));

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>);

    const textarea = await screen.findByPlaceholderText('Type a message...');
    const user = userEvent.setup();
    await user.type(textarea, 'check this out');
    await user.keyboard('{Enter}');

    // The optimistic message appears briefly, then is removed when the POST
    // rejects. Without the cleanup it would linger until the next successful
    // send wiped it via a conversation refetch.
    await waitFor(() => {
      expect(screen.queryByText('check this out')).not.toBeInTheDocument();
    });
  });
});
