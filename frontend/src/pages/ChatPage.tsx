import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import Tooltip from '@/components/ui/tooltip';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import { toast } from '@/lib/toast';
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
const LAST_SESSION_KEY = 'clawbolt:lastChatSession';

function saveLastSession(sessionId: string) {
  try { localStorage.setItem(LAST_SESSION_KEY, sessionId); } catch { /* ignore */ }
}

function loadLastSession(): string | null {
  try { return localStorage.getItem(LAST_SESSION_KEY); } catch { return null; }
}

function clearLastSession() {
  try { localStorage.removeItem(LAST_SESSION_KEY); } catch { /* ignore */ }
}

function formatSessionTime(dateStr: string): string {
  const d = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return 'Just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString();
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
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(1);
  const autoAttachDone = useRef(false);

  // Use scrollTop instead of scrollIntoView to avoid iOS Safari viewport zoom
  // bug that occurs when scrollIntoView fires during keyboard dismissal.
  const scrollToBottom = useCallback(() => {
    const el = scrollContainerRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // Prevent iOS Safari auto-zoom on input focus. Since iOS 10, maximum-scale=1
  // only blocks automatic zoom (not user pinch-zoom), so accessibility is preserved.
  // Applied only on iOS to avoid disabling pinch-zoom on Android.
  useEffect(() => {
    if (!/iPhone|iPad|iPod/.test(navigator.userAgent)) return;
    const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]');
    if (meta && !meta.content.includes('maximum-scale')) {
      meta.setAttribute('content', meta.content + ', maximum-scale=1');
    }
  }, []);

  // Close picker when clicking outside
  useEffect(() => {
    if (!pickerOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [pickerOpen]);

  // Load recent sessions, then auto-attach to last active or most recent
  useEffect(() => {
    api.listSessions(0, 50)
      .then((res) => {
        setSessions(res.sessions);
        // Auto-attach: only if no explicit ?session= param and not already done
        if (!autoAttachDone.current && !searchParams.get('session') && res.sessions.length > 0) {
          autoAttachDone.current = true;
          const saved = loadLastSession();
          const match = saved ? res.sessions.find((s) => s.id === saved) : null;
          const target = match ?? res.sessions[0];
          setActiveSessionId(target.id);
          setSearchParams({ session: target.id }, { replace: true });
        }
      })
      .catch(() => {})
      .finally(() => setLoadingSessions(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Load history when activeSessionId is set
  useEffect(() => {
    if (!activeSessionId) return;
    setLoadingHistory(true);
    saveLastSession(activeSessionId);
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

  const selectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setSearchParams({ session: sessionId }, { replace: true });
    setPickerOpen(false);
  };

  const startNewChat = () => {
    setActiveSessionId(null);
    setMessages([]);
    clearLastSession();
    setSearchParams({}, { replace: true });
    setPickerOpen(false);
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
        saveLastSession(res.session_id);
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
  const activeSession = sessions.find((s) => s.id === activeSessionId);

  return (
    <div className="flex flex-col h-full -my-4 sm:-my-6">
      {/* Header */}
      <div className="py-4 sm:py-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="heading-page">Chat</h2>
          <p className="page-subtitle">
            Talk with your AI assistant directly from the dashboard.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {loadingSessions ? (
            <Spinner className="w-4 h-4" />
          ) : (
            <>
              {/* Session picker */}
              <div className="relative" ref={pickerRef}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setPickerOpen((v) => !v)}
                  className="gap-1.5 text-base sm:text-xs max-w-[220px]"
                >
                  <span className="truncate">
                    {activeSession
                      ? (activeSession.last_message_preview
                          ? activeSession.last_message_preview.slice(0, 30) + (activeSession.last_message_preview.length > 30 ? '...' : '')
                          : formatSessionTime(activeSession.start_time))
                      : 'New conversation'}
                  </span>
                  <ChevronIcon open={pickerOpen} />
                </Button>

                {pickerOpen && (
                  <div className="absolute right-0 top-full mt-1 w-72 bg-card border border-border rounded-lg shadow-lg z-50 overflow-hidden">
                    <div className="p-1.5">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={startNewChat}
                        className="w-full justify-start gap-2 text-primary"
                      >
                        <PlusIcon />
                        New conversation
                      </Button>
                    </div>
                    {sessions.length > 0 && (
                      <>
                        <div className="border-t border-border" />
                        <div className="max-h-64 overflow-y-auto p-1.5 space-y-0.5">
                          {sessions.map((s) => (
                            <Button
                              key={s.id}
                              variant="ghost"
                              size="sm"
                              onClick={() => selectSession(s.id)}
                              className={`w-full justify-start text-left h-auto py-2 ${
                                s.id === activeSessionId
                                  ? 'bg-selected-bg text-primary'
                                  : 'text-foreground'
                              }`}
                            >
                              <div className="min-w-0">
                                <p className="text-sm truncate">
                                  {s.last_message_preview || 'Empty conversation'}
                                </p>
                                <p className="text-[10px] text-muted-foreground mt-0.5 font-normal">
                                  {formatSessionTime(s.start_time)}
                                  {s.message_count > 0 && ` | ${s.message_count} messages`}
                                </p>
                              </div>
                            </Button>
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Messages area */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto min-h-0 pb-4">
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
                  className={`max-w-[80%] px-4 py-2.5 animate-message-in ${
                    msg.role === 'user'
                      ? 'bg-primary text-white rounded-[12px_12px_4px_12px]'
                      : 'bg-card border border-border rounded-[12px_12px_12px_4px]'
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
                    <div className={msg.role === 'assistant' ? 'prose-chat' : ''}>
                      <p className="text-sm whitespace-pre-wrap">{msg.body}</p>
                    </div>
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
                <div className="bg-card border border-border rounded-[12px_12px_12px_4px] px-4 py-3 animate-message-in">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="status-dot bg-muted-foreground/60"
                      style={{ animation: 'typing-bounce 1.2s ease-in-out infinite' }}
                    />
                    <span
                      className="status-dot bg-muted-foreground/60"
                      style={{ animation: 'typing-bounce 1.2s ease-in-out 0.2s infinite' }}
                    />
                    <span
                      className="status-dot bg-muted-foreground/60"
                      style={{ animation: 'typing-bounce 1.2s ease-in-out 0.4s infinite' }}
                    />
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="pt-3 pb-4 sm:pb-6">
        <form onSubmit={handleSubmit}>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPTED_FILE_TYPES}
            onChange={handleFileSelect}
            className="hidden"
          />
          <div className="flex flex-col gap-2 p-2 bg-panel border border-border rounded-lg">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                // Auto-grow: reset height then set to scrollHeight
                const el = e.target;
                el.style.height = 'auto';
                el.style.height = Math.min(el.scrollHeight, 160) + 'px';
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !sending) {
                  e.preventDefault();
                  if (canSend) handleSubmit(e);
                }
              }}
              placeholder="Type a message..."
              disabled={sending}
              rows={1}
              className="w-full px-2 py-1.5 text-base sm:text-sm bg-transparent text-foreground placeholder:text-muted-foreground focus:outline-none resize-none disabled:opacity-50"
              autoComplete="off"
              style={{ height: 'auto' }}
            />

            {/* File preview chips */}
            {selectedFiles.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-1">
                {selectedFiles.map((file, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-1.5 bg-card border border-border text-foreground text-xs px-2 py-1 rounded-md"
                  >
                    {file.type.startsWith('image/') ? (
                      <img
                        src={URL.createObjectURL(file)}
                        alt={file.name}
                        className="w-5 h-5 rounded object-cover"
                      />
                    ) : (
                      <FileIcon />
                    )}
                    <span className="truncate max-w-[100px]">{file.name}</span>
                    <Tooltip content={`Remove ${file.name}`}>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => removeFile(i)}
                        className="ml-0.5 text-muted-foreground hover:text-foreground"
                        aria-label={`Remove ${file.name}`}
                      >
                        <CloseIcon />
                      </Button>
                    </Tooltip>
                  </div>
                ))}
              </div>
            )}

            {/* Toolbar row */}
            <div className="flex items-center justify-between">
              <Tooltip content="Attach files">
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={sending}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label="Attach files"
                >
                  <PaperclipIcon />
                </Button>
              </Tooltip>
              <Tooltip content="Send message">
                <Button
                  type="submit"
                  size="icon"
                  disabled={!canSend}
                  aria-label="Send message"
                >
                  <SendIcon />
                </Button>
              </Tooltip>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className={`w-3.5 h-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
    </svg>
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

function SendIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M5 12h14m-7-7l7 7-7 7"
      />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
    </svg>
  );
}
