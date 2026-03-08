import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import Button from '@/components/ui/button';
import Card from '@/components/ui/card';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import { toast } from 'sonner';
import type { SessionSummary } from '@/types';

interface FileAttachment {
  name: string;
  type: string;
  previewUrl?: string;
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  body: string;
  timestamp: Date;
  attachments?: FileAttachment[];
}

const ACCEPTED_FILE_TYPES = 'image/*,audio/*,application/pdf';

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
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
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

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newFiles = Array.from(e.target.files || []);
    if (newFiles.length > 0) {
      setSelectedFiles((prev) => [...prev, ...newFiles]);
    }
    // Reset so the same file can be re-selected
    e.target.value = '';
  };

  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if ((!text && selectedFiles.length === 0) || sending) return;

    // Build attachments for display
    const attachments: FileAttachment[] = selectedFiles.map((f) => ({
      name: f.name,
      type: f.type,
      previewUrl: f.type.startsWith('image/') ? URL.createObjectURL(f) : undefined,
    }));

    const userMsg: ChatMessage = {
      id: nextId.current++,
      role: 'user',
      body: text,
      timestamp: new Date(),
      attachments: attachments.length > 0 ? attachments : undefined,
    };
    setMessages((prev) => [...prev, userMsg]);

    const filesToSend = selectedFiles.length > 0 ? [...selectedFiles] : undefined;
    setInput('');
    setSelectedFiles([]);
    setSending(true);

    try {
      const res = await api.sendChatMessage(text, activeSessionId ?? undefined, filesToSend);
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
      // Re-focus input on desktop only; on mobile, programmatic focus
      // triggers iOS Safari auto-zoom and forces the keyboard open.
      if (window.matchMedia('(min-width: 640px)').matches) {
        inputRef.current?.focus();
      }
    }
  };

  const canSend = !sending && (input.trim().length > 0 || selectedFiles.length > 0);

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
                  {/* Attachments */}
                  {msg.attachments && msg.attachments.length > 0 && (
                    <div className="flex flex-wrap gap-2 mb-2">
                      {msg.attachments.map((att, i) => (
                        att.previewUrl ? (
                          <img
                            key={i}
                            src={att.previewUrl}
                            alt={att.name}
                            className="max-w-[200px] max-h-[150px] rounded object-cover"
                          />
                        ) : (
                          <div
                            key={i}
                            className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded ${
                              msg.role === 'user'
                                ? 'bg-white/20'
                                : 'bg-muted'
                            }`}
                          >
                            <FileIcon />
                            <span className="truncate max-w-[120px]">{att.name}</span>
                          </div>
                        )
                      ))}
                    </div>
                  )}
                  {msg.body && (
                    <p className="text-sm whitespace-pre-wrap">{msg.body}</p>
                  )}
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

      {/* File preview strip */}
      {selectedFiles.length > 0 && (
        <div className="flex flex-wrap gap-2 px-1 pb-2">
          {selectedFiles.map((file, i) => (
            <div
              key={i}
              className="flex items-center gap-1.5 bg-muted text-foreground text-xs px-2 py-1 rounded"
            >
              {file.type.startsWith('image/') ? (
                <img
                  src={URL.createObjectURL(file)}
                  alt={file.name}
                  className="w-6 h-6 rounded object-cover"
                />
              ) : (
                <FileIcon />
              )}
              <span className="truncate max-w-[100px]">{file.name}</span>
              <button
                type="button"
                onClick={() => removeFile(i)}
                className="ml-0.5 text-muted-foreground hover:text-foreground"
              >
                x
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Input area */}
      <div className="border-t border-border pt-4 pb-4 sm:pb-6">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPTED_FILE_TYPES}
            onChange={handleFileSelect}
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={sending}
            className="px-2 py-2 text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
            title="Attach files"
          >
            <PaperclipIcon />
          </button>
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type a message..."
            disabled={sending}
            className="flex-1 px-3 py-2.5 sm:py-2 text-base sm:text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors disabled:opacity-50"
            autoComplete="off"
          />
          <Button type="submit" disabled={!canSend}>
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

function PaperclipIcon() {
  return (
    <svg
      className="w-5 h-5"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
      />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg
      className="w-4 h-4 shrink-0"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"
      />
    </svg>
  );
}
