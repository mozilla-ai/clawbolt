import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import { ChatActivityProvider } from '@/contexts/ChatActivityContext';
import ChatPage from './ChatPage';

// Mock the api module
vi.mock('@/api', () => ({
  default: {
    getSession: vi.fn(),
    sendChatMessage: vi.fn(),
    listSessions: vi.fn().mockResolvedValue({ total: 0, items: [] }),
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
    mockApi.getSession.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      is_active: true,
      channel: 'webchat',
      initial_system_prompt: '',
      last_compacted_seq: 0,
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
    mockApi.getSession.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      is_active: true,
      channel: 'webchat',
      initial_system_prompt: '',
      last_compacted_seq: 0,
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
    mockApi.getSession.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      is_active: true,
      channel: 'webchat',
      initial_system_prompt: '',
      last_compacted_seq: 0,
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

describe('ChatPage session auto-discovery', () => {
  it('discovers the most recent active session when no session is saved', async () => {
    const sessionId = '1_3000';
    mockApi.listSessions.mockResolvedValue({
      total: 1,
      items: [
        {
          session_id: sessionId,
          channel: 'webchat',
          is_active: true,
          message_count: 2,
          created_at: '2025-01-01T00:00:00Z',
          last_message_at: '2025-01-01T00:01:00Z',
        },
      ],
    });
    mockApi.getSession.mockResolvedValue({
      session_id: sessionId,
      user_id: '1',
      created_at: '2025-01-01T00:00:00Z',
      last_message_at: '2025-01-01T00:01:00Z',
      is_active: true,
      channel: 'webchat',
      initial_system_prompt: '',
      last_compacted_seq: 0,
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

    // Render without ?session= param and with empty localStorage
    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: '/app/chat' });

    // Should discover the session and load its history
    await waitFor(() => {
      expect(mockApi.listSessions).toHaveBeenCalledWith({ is_active: true, limit: 1 });
    });

    await waitFor(() => {
      expect(screen.getByText('Previous message')).toBeInTheDocument();
      expect(screen.getByText('Previous reply')).toBeInTheDocument();
    });
  });

  it('shows empty state when no active sessions exist', async () => {
    mockApi.listSessions.mockResolvedValue({ total: 0, items: [] });

    renderWithRouter(<ChatActivityProvider><ChatPage /></ChatActivityProvider>, { route: '/app/chat' });

    await waitFor(() => {
      expect(mockApi.listSessions).toHaveBeenCalled();
    });

    // Should show the empty state prompt
    expect(screen.getByText('Send a message to start chatting.')).toBeInTheDocument();
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
