import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import Button from '@/components/ui/button';
import { Tooltip } from '@heroui/tooltip';
import { Spinner } from '@heroui/spinner';
import api from '@/api';
import { toast } from '@/lib/toast';
import { useSessions, useSession } from '@/hooks/queries';
import { queryKeys } from '@/lib/query-keys';
import type { SessionSummary, ToolInteraction } from '@/types';

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
  toolInteractions?: ToolInteraction[];
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
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    searchParams.get('session'),
  );
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [currentTool, setCurrentTool] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const forceNewRef = useRef(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(1);
  const autoAttachDone = useRef(false);
  const mountedRef = useRef(true);

  // Track mounted state to prevent state updates after unmount
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Fetch sessions via React Query
  const { data: sessionsData, isPending: loadingSessions } = useSessions(0, 50);
  const sessions: SessionSummary[] = sessionsData?.sessions ?? [];

  // Fetch session history via React Query (poll every 3s when idle)
  const { data: sessionDetail, isPending: loadingHistoryPending, isError: historyError } = useSession(
    activeSessionId,
    sending ? false : 3_000,
  );
  const loadingHistory = loadingHistoryPending && !!activeSessionId;

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

  // Auto-attach to last active or most recent session when sessions load
  useEffect(() => {
    if (autoAttachDone.current || !sessionsData || searchParams.get('session')) return;
    autoAttachDone.current = true;
    if (sessionsData.sessions.length > 0) {
      const saved = loadLastSession();
      const match = saved ? sessionsData.sessions.find((s) => s.id === saved) : null;
      const target = match ?? sessionsData.sessions[0];
      if (target) {
        setActiveSessionId(target.id);
        setSearchParams({ session: target.id }, { replace: true });
      }
    }
  }, [sessionsData, searchParams, setSearchParams]);

  // Save active session to localStorage
  useEffect(() => {
    if (activeSessionId) saveLastSession(activeSessionId);
  }, [activeSessionId]);

  // Populate messages from session history when it loads
  useEffect(() => {
    if (!sessionDetail) return;
    const loaded: ChatMessage[] = sessionDetail.messages.map((m) => ({
      id: nextId.current++,
      role: m.direction === 'inbound' ? 'user' : 'assistant',
      body: m.body,
      timestamp: new Date(m.timestamp),
      toolInteractions: m.tool_interactions && m.tool_interactions.length > 0 ? m.tool_interactions : undefined,
    }));
    setMessages(loaded);
  }, [sessionDetail]);

  // Handle history load errors
  useEffect(() => {
    if (historyError && activeSessionId) {
      toast.error('Failed to load session');
      setActiveSessionId(null);
      setSearchParams({}, { replace: true });
    }
  }, [historyError, activeSessionId, setSearchParams]);

  const selectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setSearchParams({ session: sessionId }, { replace: true });
    setPickerOpen(false);
    forceNewRef.current = false;
  };

  const startNewChat = () => {
    setActiveSessionId(null);
    setMessages([]);
    clearLastSession();
    setSearchParams({}, { replace: true });
    setPickerOpen(false);
    forceNewRef.current = true;
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
      const toolNames: string[] = [];
      const res = await api.sendChatMessage(
        text,
        activeSessionId ?? undefined,
        filesToSend,
        (event) => {
          if (!mountedRef.current) return;
          if (event.type === 'tool_call') {
            setCurrentTool(event.tool_name ?? null);
            if (event.tool_name) {
              toolNames.push(event.tool_name);
            }
          }
        },
        forceNewRef.current,
      );
      if (!mountedRef.current) return;
      const assistantMsg: ChatMessage = {
        id: nextId.current++,
        role: 'assistant',
        body: res.reply,
        timestamp: new Date(),
        toolInteractions: toolNames.length > 0
          ? toolNames.map((name) => ({ name }))
          : undefined,
      };
      setMessages((prev) => [...prev, assistantMsg]);

      if (!activeSessionId) {
        setActiveSessionId(res.session_id);
        setSearchParams({ session: res.session_id }, { replace: true });
        saveLastSession(res.session_id);
        forceNewRef.current = false;
      }
      // Refresh session data so full tool interactions from the DB replace
      // the partial names collected from SSE events
      void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.sessions.detail(res.session_id),
      });
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      const msg = err instanceof Error ? err.message : 'Failed to send message';
      toast.error(msg);
    } finally {
      if (!mountedRef.current) return;
      setSending(false);
      setCurrentTool(null);
      // Re-focus input on desktop only; on mobile, programmatic focus
      // triggers iOS Safari auto-zoom and forces the keyboard open.
      // Deferred via requestAnimationFrame so React flushes the
      // setSending(false) render before we focus the (now enabled) textarea.
      if (window.matchMedia('(min-width: 640px)').matches) {
        requestAnimationFrame(() => inputRef.current?.focus());
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
          <h2 className="text-xl font-semibold">Chat</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Talk with your AI assistant directly from the dashboard.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {loadingSessions ? (
            <Spinner color="primary" size="sm" aria-label="Loading" />
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
        {loadingHistory && !sessionDetail ? (
          <div className="flex justify-center py-12"><Spinner color="primary" size="md" aria-label="Loading" /></div>
        ) : messages.length === 0 ? (
          <div className="text-center py-12 text-muted-foreground">
            <ChatBubbleIcon />
            <p className="text-sm mt-3">Send a message to start chatting.</p>
          </div>
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

                  {msg.toolInteractions && msg.toolInteractions.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {msg.toolInteractions.map((tool, i) => (
                        <div
                          key={i}
                          className={`text-xs px-2 py-1 rounded ${
                            msg.role === 'user'
                              ? 'bg-white/10'
                              : 'bg-panel'
                          }`}
                        >
                          <span className="font-medium">Tool: </span>
                          {String(tool['name'] ?? tool['tool'] ?? 'unknown')}
                          {'result' in tool && (
                            <span className="opacity-70">
                              {' '}- {String(tool['result']).slice(0, 100)}
                            </span>
                          )}
                        </div>
                      ))}
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
              <ToolUseIndicator toolName={currentTool ?? undefined} />
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
                    <Tooltip content={`Remove ${file.name}`} delay={400} closeDelay={0}>
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
              <Tooltip content="Attach files" delay={400} closeDelay={0}>
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
              <Tooltip content="Send message" delay={400} closeDelay={0}>
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

function ToolUseIndicator({ toolName }: { toolName?: string }) {
  return (
    <div className="flex justify-start">
      <div className="bg-card border border-border rounded-[12px_12px_12px_4px] px-4 py-3 animate-message-in">
        <div className="flex items-center gap-2">
          <Spinner color="primary" size="sm" aria-label="Loading" />
          <span className="text-xs text-muted-foreground">
            {toolName ? `Using ${toolName}...` : 'Thinking...'}
          </span>
        </div>
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
