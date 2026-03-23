import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '@/test/test-utils';
import ChatPage from './ChatPage';

// Mock the api module
vi.mock('@/api', () => ({
  default: {
    getSession: vi.fn(),
    sendChatMessage: vi.fn(),
  },
}));

// Re-import after mock so we can control return values
import api from '@/api';
const mockApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
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

    renderWithRouter(<ChatPage />, { route: `/app/chat?session=${sessionId}` });

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

    renderWithRouter(<ChatPage />, { route: `/app/chat?session=${sessionId}` });

    await waitFor(() => {
      expect(screen.getByText('Hi there!')).toBeInTheDocument();
    });

    // No "Tool:" labels should appear
    expect(screen.queryByText('Tool:')).not.toBeInTheDocument();
  });
});

describe('ChatPage concurrent messaging', () => {
  it('keeps input and send button enabled while assistant is responding', async () => {
    // sendChatMessage never resolves, simulating a pending response
    mockApi.sendChatMessage.mockReturnValue(new Promise(() => {}));

    renderWithRouter(<ChatPage />);

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
