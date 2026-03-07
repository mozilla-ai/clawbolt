import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import Button from '@/components/ui/button';
import Card from '@/components/ui/card';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import { toast } from 'sonner';
import type { SessionSummary } from '@/types';

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  body: string;
  timestamp: Date;
}

export default function ChatPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    searchParams.get('session'),
  );
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // Load recent sessions for the selector
  useEffect(() => {
    api.listSessions(0, 50)
      .then((res) => setSessions(res.sessions))
      .catch(() => {})
      .finally(() => setLoadingSessions(false));
  }, []);

  // Load history when activeSessionId is set
  useEffect(() => {
    if (!activeSessionId) return;
    setLoadingHistory(true);
    api.getSession(activeSessionId)
      .then((detail) => {
        const loaded: ChatMessage[] = detail.messages.map((m) => ({
          id: nextId.current++,
          role: m.direction === 'inbound' ? 'user' : 'assistant',
          body: m.body,
          timestamp: new Date(m.timestamp),
        }));
        setMessages(loaded);
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : 'Failed to load session';
        toast.error(msg);
        setActiveSessionId(null);
        setSearchParams({}, { replace: true });
      })
      .finally(() => setLoadingHistory(false));
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSessionChange = (value: string) => {
    if (value === '__new__') {
      setActiveSessionId(null);
      setMessages([]);
      setSearchParams({}, { replace: true });
    } else {
      setActiveSessionId(value);
      setSearchParams({ session: value }, { replace: true });
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;

    const userMsg: ChatMessage = {
      id: nextId.current++,
      role: 'user',
      body: text,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setSending(true);

    try {
      const res = await api.sendChatMessage(text, activeSessionId ?? undefined);
      const assistantMsg: ChatMessage = {
        id: nextId.current++,
        role: 'assistant',
        body: res.reply,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, assistantMsg]);

      if (!activeSessionId) {
        setActiveSessionId(res.session_id);
        setSearchParams({ session: res.session_id }, { replace: true });
        // Refresh session list to include the new session
        api.listSessions(0, 50)
          .then((r) => setSessions(r.sessions))
          .catch(() => {});
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to send message';
      toast.error(msg);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="flex flex-col h-full -my-4 sm:-my-6">
      {/* Header */}
      <div className="py-4 sm:py-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">Chat</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Talk with your AI assistant directly from the dashboard.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {loadingSessions ? (
            <Spinner className="w-4 h-4" />
          ) : (
            <select
              value={activeSessionId ?? '__new__'}
              onChange={(e) => handleSessionChange(e.target.value)}
              className="px-2 py-1.5 text-xs bg-card border border-border rounded-[--radius-md] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors max-w-[200px]"
            >
              <option value="__new__">New conversation</option>
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.last_message_preview
                    ? s.last_message_preview.slice(0, 40) + (s.last_message_preview.length > 40 ? '...' : '')
                    : new Date(s.start_time).toLocaleDateString()}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto min-h-0 pb-4">
        {loadingHistory ? (
          <div className="flex justify-center py-12"><Spinner /></div>
        ) : messages.length === 0 ? (
          <Card className="text-center py-12">
            <div className="text-muted-foreground">
              <ChatBubbleIcon />
              <p className="text-sm mt-3">Send a message to start chatting.</p>
            </div>
          </Card>
        ) : (
          <div className="space-y-3">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] rounded-[--radius-lg] px-4 py-2.5 ${
                    msg.role === 'user'
                      ? 'bg-primary text-white'
                      : 'bg-card border border-border'
                  }`}
                >
                  <p className="text-sm whitespace-pre-wrap">{msg.body}</p>
                  <p
                    className={`text-[10px] mt-1 ${
                      msg.role === 'user' ? 'text-white/60' : 'text-muted-foreground'
                    }`}
                  >
                    {msg.timestamp.toLocaleTimeString()}
                  </p>
                </div>
              </div>
            ))}

            {sending && (
              <div className="flex justify-start">
                <div className="bg-card border border-border rounded-[--radius-lg] px-4 py-2.5">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Spinner className="w-4 h-4" />
                    Thinking...
                  </div>
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="border-t border-border pt-4 pb-4 sm:pb-6">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type a message..."
            disabled={sending}
            className="flex-1 px-3 py-2.5 sm:py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors disabled:opacity-50"
            autoComplete="off"
          />
          <Button type="submit" disabled={sending || !input.trim()}>
            Send
          </Button>
        </form>
      </div>
    </div>
  );
}

function ChatBubbleIcon() {
  return (
    <svg
      className="w-10 h-10 mx-auto text-muted-foreground/50"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
      />
    </svg>
  );
}
