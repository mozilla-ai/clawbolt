import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import Markdown from 'react-markdown';
import Button from '@/components/ui/button';
import ConfirmModal from '@/components/ui/confirm-modal';
import { Checkbox } from '@heroui/checkbox';
import { Tooltip } from '@heroui/tooltip';
import { Spinner } from '@heroui/spinner';
import api from '@/api';
import { toast } from '@/lib/toast';
import { useSession } from '@/hooks/queries';
import { queryKeys } from '@/lib/query-keys';
import type { ToolInteraction } from '@/types';

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
  seq?: number;
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

export default function ChatPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [pendingCount, setPendingCount] = useState(0);
  const pendingRef = useRef(0);
  const sending = pendingCount > 0;
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    searchParams.get('session'),
  );
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [currentTool, setCurrentTool] = useState<string | null>(null);
  const [activityTool, setActivityTool] = useState<string | null>(null);
  const [agentBusy, setAgentBusy] = useState(false);
  const [waitingForApproval, setWaitingForApproval] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deletingMsgId, setDeletingMsgId] = useState<number | null>(null);
  const [systemPromptOpen, setSystemPromptOpen] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedSeqs, setSelectedSeqs] = useState<Set<number>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);
  const [batchExitingSeqs, setBatchExitingSeqs] = useState<Set<number>>(new Set());
  const [confirmModal, setConfirmModal] = useState<{
    title: string;
    message: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);
  const autoAttachDone = useRef(false);
  const mountedRef = useRef(true);

  // Track mounted state to prevent state updates after unmount
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Subscribe to user-level activity stream for real-time agent status
  // from any channel (Telegram, iMessage, etc.)
  useEffect(() => {
    const controller = api.subscribeToActivity((event) => {
      if (!mountedRef.current) return;
      if (event.type === 'thinking') {
        setAgentBusy(true);
        setActivityTool(null);
      } else if (event.type === 'tool_call') {
        setAgentBusy(true);
        setActivityTool(event.tool_name ?? null);
      } else if (event.type === 'done') {
        setAgentBusy(false);
        setActivityTool(null);
        // Refresh session data to pick up the new message, but only when
        // webchat isn't actively waiting for its own SSE reply. When sending,
        // handleSubmit adds the reply and invalidates queries itself. Without
        // this guard, the activity refresh can load the reply from the DB
        // before the SSE resolves, and then the SSE handler appends a duplicate.
        if (pendingRef.current === 0) {
          void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
        }
      }
    });
    return () => controller.abort();
  }, [queryClient]);

  // Fetch session history via React Query.  The activity SSE stream
  // already invalidates queries on the "done" event, so periodic
  // polling is unnecessary and wasteful.
  const { data: sessionDetail, isPending: loadingHistoryPending, isError: historyError } = useSession(
    activeSessionId,
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

  // Auto-focus the chat input when the page mounts or is navigated to
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Auto-attach to last active session from localStorage, or discover from API
  useEffect(() => {
    if (autoAttachDone.current || searchParams.get('session')) return;
    autoAttachDone.current = true;
    const saved = loadLastSession();
    if (saved) {
      setActiveSessionId(saved);
      setSearchParams({ session: saved }, { replace: true });
      return;
    }
    // No saved session: discover the most recent active session from the backend
    api.listSessions({ is_active: true, limit: 1 }).then((res) => {
      if (!mountedRef.current) return;
      const latest = res.items[0];
      if (latest) {
        setActiveSessionId(latest.session_id);
        setSearchParams({ session: latest.session_id }, { replace: true });
        saveLastSession(latest.session_id);
      }
    }).catch(() => {
      // Silently ignore: user may not have any sessions yet
    });
  }, [searchParams, setSearchParams]);

  // Save active session to localStorage and reset expand/selection state
  useEffect(() => {
    if (activeSessionId) saveLastSession(activeSessionId);
    setExpandedTools(new Set());
    setSelectionMode(false);
    setSelectedSeqs(new Set());
  }, [activeSessionId]);

  // Populate messages from session history when it loads
  useEffect(() => {
    if (!sessionDetail) return;
    const loaded: ChatMessage[] = sessionDetail.messages.map((m) => ({
      id: nextId.current++,
      role: m.direction === 'inbound' ? 'user' : 'assistant',
      body: m.body,
      timestamp: new Date(m.timestamp),
      seq: m.seq,
      toolInteractions: m.tool_interactions && m.tool_interactions.length > 0 ? m.tool_interactions : undefined,
    }));
    setMessages(loaded);
  }, [sessionDetail]);

  // Handle history load errors (e.g. stale session in localStorage)
  useEffect(() => {
    if (historyError && activeSessionId) {
      try { localStorage.removeItem(LAST_SESSION_KEY); } catch { /* ignore */ }
      setActiveSessionId(null);
      setSearchParams({}, { replace: true });
    }
  }, [historyError, activeSessionId, setSearchParams]);

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
    if (!text && selectedFiles.length === 0) return;

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
    // Capture session state at submit time so concurrent sends use correct values
    const submitSessionId = activeSessionId;
    setInput('');
    setSelectedFiles([]);
    pendingRef.current++;
    setPendingCount((c) => c + 1);

    try {
      const toolNames: string[] = [];
      const res = await api.sendChatMessage(
        text,
        submitSessionId ?? undefined,
        filesToSend,
        (event) => {
          if (!mountedRef.current) return;
          if (event.type === 'tool_call') {
            setCurrentTool(event.tool_name ?? null);
            if (event.tool_name) {
              toolNames.push(event.tool_name);
            }
          } else if (event.type === 'approval_request') {
            // Display approval requests as regular assistant messages
            // so the user replies by typing (like Telegram/iMessage)
            const approvalMsg: ChatMessage = {
              id: nextId.current++,
              role: 'assistant',
              body: event.content ?? '',
              timestamp: new Date(),
            };
            setMessages((prev) => [...prev, approvalMsg]);
            setCurrentTool(null);
            setWaitingForApproval(true);
          }
        },
        (accepted) => {
          if (!mountedRef.current) return;
          // Capture session ID immediately so follow-up messages use it
          if (!submitSessionId && accepted.session_id) {
            setActiveSessionId(accepted.session_id);
            setSearchParams({ session: accepted.session_id }, { replace: true });
            saveLastSession(accepted.session_id);
          }
        },
      );
      if (!mountedRef.current) return;
      // Skip adding an assistant message when the reply is empty
      // (the agent chose not to respond, e.g. user asked for silence).
      if (res.reply) {
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
      pendingRef.current--;
      setPendingCount((c) => c - 1);
      // Only clear indicators when all pending requests are done
      if (pendingRef.current === 0) {
        setCurrentTool(null);
        setWaitingForApproval(false);
      }
    }
  };

  const toggleToolExpand = (key: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleDeleteMessage = async (msg: ChatMessage) => {
    if (!activeSessionId || !msg.seq || deletingMsgId !== null) return;
    try {
      await api.deleteMessage(activeSessionId, msg.seq);
      // API succeeded: play exit animation, then remove from state
      setDeletingMsgId(msg.id);
      await new Promise((r) => setTimeout(r, 250));
      setMessages((prev) => prev.filter((m) => m.id !== msg.id));
      setDeletingMsgId(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.sessions.detail(activeSessionId),
      });
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : 'Failed to delete message';
      toast.error(errMsg);
    }
  };

  const toggleSelection = (seq: number) => {
    setSelectedSeqs((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

  const exitSelectionMode = () => {
    setSelectionMode(false);
    setSelectedSeqs(new Set());
  };

  const selectableMessages = messages.filter((m) => m.seq);

  const handleBatchDelete = async () => {
    if (!activeSessionId || selectedSeqs.size === 0 || isBatchDeleting) return;
    // Filter to only seqs that still exist in messages
    const validSeqs = [...selectedSeqs].filter((seq) =>
      messages.some((m) => m.seq === seq),
    );
    if (validSeqs.length === 0) return;
    setIsBatchDeleting(true);
    setConfirmModal(null);
    try {
      await api.deleteMessages(activeSessionId, validSeqs);
      // API succeeded: now play exit animation
      const idsToRemove = messages
        .filter((m) => m.seq && validSeqs.includes(m.seq))
        .map((m) => m.id);
      setBatchExitingSeqs(new Set(validSeqs));
      await new Promise((r) => setTimeout(r, 250));
      setMessages((prev) => prev.filter((m) => !idsToRemove.includes(m.id)));
      setBatchExitingSeqs(new Set());
      void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.sessions.detail(activeSessionId),
      });
      toast.success(`Deleted ${validSeqs.length} message${validSeqs.length > 1 ? 's' : ''}`);
      exitSelectionMode();
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : 'Failed to delete messages';
      toast.error(errMsg);
    } finally {
      setIsBatchDeleting(false);
    }
  };

  const canSend = input.trim().length > 0 || selectedFiles.length > 0;

  return (
    <div className="flex flex-col h-full -my-4 sm:-my-6">
      {/* Header */}
      <div className="py-4 sm:py-6 flex items-start justify-between">
        <div>
          <h2 className="text-xl font-semibold font-display">Chat</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Talk with your AI assistant directly from the dashboard.
          </p>
        </div>
        {activeSessionId && messages.length > 0 && (
          <div className="flex items-center gap-1.5">
            <Tooltip content={selectionMode ? 'Exit selection' : 'Select messages'} delay={400} closeDelay={0}>
              <Button
                variant={selectionMode ? 'secondary' : 'ghost'}
                size="sm"
                disabled={isDeleting || sending || isBatchDeleting}
                className="text-muted-foreground shrink-0"
                onClick={() => selectionMode ? exitSelectionMode() : setSelectionMode(true)}
              >
                <SelectIcon />
                <span className="ml-1.5 hidden sm:inline">{selectionMode ? 'Cancel' : 'Select'}</span>
              </Button>
            </Tooltip>
            {!selectionMode && (
              <Tooltip content="Delete conversation history" delay={400} closeDelay={0}>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={isDeleting || sending}
                  className="text-muted-foreground hover:text-danger shrink-0"
                  onClick={() => {
                    if (!activeSessionId || isDeleting) return;
                    setConfirmModal({
                      title: 'Clear conversation history',
                      message: 'Delete all conversation messages? Your memory and personality will be kept.',
                      confirmLabel: 'Clear all',
                      onConfirm: async () => {
                        setConfirmModal(null);
                        setIsDeleting(true);
                        try {
                          await api.deleteConversationHistory(activeSessionId);
                          setMessages([]);
                          void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
                          void queryClient.invalidateQueries({
                            queryKey: queryKeys.sessions.detail(activeSessionId),
                          });
                          toast.success('Conversation history deleted');
                        } catch (err: unknown) {
                          const msg = err instanceof Error ? err.message : 'Failed to delete history';
                          toast.error(msg);
                        } finally {
                          setIsDeleting(false);
                        }
                      },
                    });
                  }}
                >
                  <TrashIcon />
                  <span className="ml-1.5 hidden sm:inline">Clear history</span>
                </Button>
              </Tooltip>
            )}
          </div>
        )}
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
            {sessionDetail?.initial_system_prompt && (
              <div className="border border-border rounded-lg overflow-hidden">
                <button
                  type="button"
                  onClick={() => setSystemPromptOpen((o) => !o)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:bg-panel transition-colors"
                >
                  <ChevronIcon open={systemPromptOpen} />
                  <span className="font-medium">System Prompt</span>
                </button>
                {systemPromptOpen && (
                  <div className="px-3 pb-3 text-xs text-muted-foreground whitespace-pre-wrap border-t border-border bg-panel/50">
                    {sessionDetail.initial_system_prompt}
                  </div>
                )}
              </div>
            )}
            {messages.map((msg, idx) => {
              const lastCompactedSeq = sessionDetail?.last_compacted_seq ?? 0;
              const prevSeq = idx > 0 ? (messages[idx - 1]?.seq ?? 0) : 0;
              const showCompactionMarker =
                lastCompactedSeq > 0 &&
                msg.seq !== undefined &&
                msg.seq > lastCompactedSeq &&
                (idx === 0 || prevSeq <= lastCompactedSeq);
              const isExiting = batchExitingSeqs.size > 0
                ? (msg.seq !== undefined && batchExitingSeqs.has(msg.seq))
                : deletingMsgId === msg.id;
              return (
                <div
                  key={msg.id}
                  style={isExiting ? { animation: 'message-out 250ms ease-in forwards' } : undefined}
                >
                  {showCompactionMarker && <CompactionMarker />}
                  <div className={`group/msg flex items-center gap-1.5 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                    {selectionMode && msg.seq ? (
                      <Checkbox
                        isSelected={selectedSeqs.has(msg.seq)}
                        onValueChange={() => toggleSelection(msg.seq!)}
                        aria-label={`Select message ${msg.seq}`}
                        className="shrink-0"
                        size="sm"
                      />
                    ) : msg.role === 'user' && msg.seq && !selectionMode ? (
                      <button
                        type="button"
                        onClick={() => handleDeleteMessage(msg)}
                        disabled={deletingMsgId !== null}
                        className="opacity-0 group-hover/msg:opacity-100 focus-visible:opacity-100 transition-opacity duration-150 p-1 rounded text-muted-foreground hover:text-danger shrink-0"
                        aria-label="Delete message"
                      >
                        <SmallTrashIcon />
                      </button>
                    ) : null}
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
                    msg.role === 'assistant' ? (
                      <div className="prose-chat">
                        <Markdown>{msg.body}</Markdown>
                      </div>
                    ) : (
                      <p className="text-sm whitespace-pre-wrap">{msg.body}</p>
                    )
                  )}

                  {msg.toolInteractions && msg.toolInteractions.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {msg.toolInteractions.map((tool, i) => {
                        const toolName = String(tool['name'] ?? tool['tool'] ?? 'unknown');
                        const result = 'result' in tool ? String(tool['result']) : '';
                        const args = tool['args'] as Record<string, unknown> | undefined;
                        const hasArgs = args && Object.keys(args).length > 0;
                        const isError = tool['is_error'] === true;
                        const toolCallId = tool['tool_call_id'] as string | undefined;
                        const expandKey = `${msg.seq ?? msg.id}-${i}`;
                        const isExpanded = expandedTools.has(expandKey);
                        const hasDetails = 'result' in tool || hasArgs;

                        return (
                          <div
                            key={i}
                            className={`rounded text-[13px] ${
                              msg.role === 'user'
                                ? 'bg-white/10'
                                : isError
                                  ? 'bg-danger/5'
                                  : 'bg-panel'
                            }`}
                          >
                            <button
                              type="button"
                              onClick={() => hasDetails && toggleToolExpand(expandKey)}
                              aria-expanded={hasDetails ? isExpanded : undefined}
                              className={`w-full flex items-center gap-1.5 px-2 py-1.5 text-left ${
                                hasDetails ? 'cursor-pointer' : 'cursor-default'
                              }`}
                            >
                              {hasDetails && (
                                <svg
                                  className={`w-3 h-3 shrink-0 transition-transform duration-150 ${
                                    isExpanded ? 'rotate-90' : ''
                                  }`}
                                  fill="none"
                                  stroke="currentColor"
                                  viewBox="0 0 24 24"
                                >
                                  <path
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth={2}
                                    d="M9 5l7 7-7 7"
                                  />
                                </svg>
                              )}
                              <span className="font-medium">{toolName}</span>
                              {isError && (
                                <span className="text-[12px] font-medium text-danger">
                                  Error
                                </span>
                              )}
                              {!isExpanded && result && (
                                <span className="opacity-50 truncate text-xs">
                                  {result.length > 80
                                    ? result.slice(0, 80) + '...'
                                    : result}
                                </span>
                              )}
                            </button>
                            {isExpanded && (
                              <div className="px-2 pb-2 space-y-2">
                                <div className="font-mono text-[14px] whitespace-pre-wrap max-h-60 overflow-y-auto bg-panel/50 rounded px-2 py-1.5">
                                  {result || 'No result'}
                                </div>
                                {hasArgs && (
                                  <div>
                                    <span className="text-xs font-medium opacity-70">
                                      Args
                                    </span>
                                    <pre className="font-mono text-[14px] whitespace-pre-wrap max-h-40 overflow-y-auto bg-panel/50 rounded px-2 py-1.5 mt-0.5">
                                      {(() => { try { return JSON.stringify(args, null, 2); } catch { return String(args); } })()}
                                    </pre>
                                  </div>
                                )}
                                {toolCallId && (
                                  <p className="text-[11px] opacity-40">
                                    {toolCallId}
                                  </p>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  <p
                    className={`text-[10px] mt-1 ${
                      msg.role === 'user' ? 'text-white/60' : 'text-muted-foreground'
                    }`}
                  >
                    {msg.timestamp.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                  </p>
                </div>
                    {!selectionMode && msg.role === 'assistant' && msg.seq && (
                      <button
                        type="button"
                        onClick={() => handleDeleteMessage(msg)}
                        disabled={deletingMsgId !== null}
                        className="opacity-0 group-hover/msg:opacity-100 focus-visible:opacity-100 transition-opacity duration-150 p-1 rounded text-muted-foreground hover:text-danger shrink-0"
                        aria-label="Delete message"
                      >
                        <SmallTrashIcon />
                      </button>
                    )}
              </div>
                </div>
              );
            })}

            {sending && !waitingForApproval && (
              <ToolUseIndicator toolName={currentTool ?? undefined} />
            )}
            {!sending && agentBusy && (
              <ToolUseIndicator toolName={activityTool ?? undefined} />
            )}
          </div>
        )}
      </div>

      {/* Selection action bar */}
      {selectionMode && (
        <div className="border-t border-border bg-card/95 backdrop-blur-sm px-4 py-2.5 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground">
              {selectedSeqs.size} selected
            </span>
            <button
              type="button"
              onClick={() => {
                if (selectedSeqs.size === selectableMessages.length) {
                  setSelectedSeqs(new Set());
                } else {
                  setSelectedSeqs(new Set(selectableMessages.map((m) => m.seq!)));
                }
              }}
              className="text-xs text-primary hover:text-primary-hover underline"
            >
              {selectedSeqs.size === selectableMessages.length ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          <Button
            variant="danger"
            size="sm"
            disabled={selectedSeqs.size === 0 || isBatchDeleting}
            isLoading={isBatchDeleting}
            onClick={() => {
              setConfirmModal({
                title: 'Delete messages',
                message: `Delete ${selectedSeqs.size} selected message${selectedSeqs.size > 1 ? 's' : ''}? This cannot be undone.`,
                confirmLabel: `Delete ${selectedSeqs.size}`,
                onConfirm: handleBatchDelete,
              });
            }}
          >
            <SmallTrashIcon />
            <span className="ml-1">Delete</span>
          </Button>
        </div>
      )}

      {/* Confirm modal */}
      <ConfirmModal
        isOpen={confirmModal !== null}
        onConfirm={confirmModal?.onConfirm ?? (() => {})}
        onCancel={() => setConfirmModal(null)}
        title={confirmModal?.title ?? ''}
        message={confirmModal?.message ?? ''}
        confirmLabel={confirmModal?.confirmLabel}
        variant="danger"
        isLoading={isBatchDeleting || isDeleting}
      />

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
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  if (canSend) handleSubmit(e);
                }
              }}
              placeholder="Type a message..."
              rows={1}
              className="w-full px-2 py-1.5 text-base sm:text-sm bg-transparent text-foreground placeholder:text-muted-foreground focus:outline-none resize-none"
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

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className={`w-3 h-3 transition-transform ${open ? 'rotate-90' : ''}`}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
    </svg>
  );
}

function SelectIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
      />
    </svg>
  );
}

function SmallTrashIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.5}
        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
      />
    </svg>
  );
}

function CompactionMarker() {
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="flex-1 border-t border-dashed border-muted-foreground/30" />
      <span className="text-[10px] text-muted-foreground">compacted above</span>
      <div className="flex-1 border-t border-dashed border-muted-foreground/30" />
    </div>
  );
}
