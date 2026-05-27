import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import { useOutletContext } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import Button from '@/components/ui/button';
import ConfirmModal from '@/components/ui/confirm-modal';
import { Checkbox } from '@heroui/checkbox';
import { Tooltip } from '@heroui/tooltip';
import { Spinner } from '@heroui/spinner';
import api from '@/api';
import { compressImageIfNeeded, shouldCompressImage } from '@/lib/imageCompression';
import { toast } from '@/lib/toast';
import { useAppConfig, useConversation, useConversationSystemPrompt } from '@/hooks/queries';
import { queryKeys } from '@/lib/query-keys';
import { useChatActivity } from '@/contexts/ChatActivityContext';
import type { AppShellContext } from '@/layouts/AppShell';
import type { ToolInteraction } from '@/types';

interface FileAttachment {
  name: string;
  type: string;
  previewUrl?: string;
}

/**
 * A staged file plus its blob preview URL. The URL is created once when the
 * file is selected and lives until the file is either removed from the chip
 * row or transferred onto a sent message. Critically, the URL is NOT
 * regenerated on render; the previous implementation called
 * URL.createObjectURL() inside the chip JSX, which produced a new blob URL
 * on every keystroke (since typing re-renders ChatPage) and caused noticeable
 * input lag for phone-sized photos. #1368.
 */
interface SelectedFile {
  file: File;
  previewUrl?: string;
  // True while a background compression pass is replacing `file` with a
  // smaller JPEG re-encoding. The chip stays in the row throughout; on
  // submit we await any in-flight compressions so the upload uses the
  // shrunken bytes. #1368.
  compressing?: boolean;
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  body: string;
  timestamp: Date;
  seq?: number;
  attachments?: FileAttachment[];
  toolInteractions?: ToolInteraction[];
  // Upload tracking for optimistic outbound messages that include files.
  // `undefined` means the message is either already sent or has nothing to
  // upload (text-only). #1368.
  uploadState?: 'uploading' | 'failed';
  uploadProgress?: number; // 0..1, only meaningful while `uploading`.
  uploadAbort?: AbortController;
  // What to resend on retry: the original text and the original File handles
  // (NOT the displayed FileAttachment, which carries only metadata + blob URL).
  uploadOriginals?: { text: string; files: File[] };
}

const ACCEPTED_FILE_TYPES = 'image/*,audio/*,application/pdf';

export default function ChatPage() {
  const queryClient = useQueryClient();
  // The system prompt panel exposes the operator's preamble and tool wiring,
  // so on multi-tenant premium deployments it is admin-only. OSS standalone
  // (single-tenant) has no admin/user split, so the panel is visible there.
  // useOutletContext is undefined under MemoryRouter in tests; default both
  // flags to false so the OSS standalone branch (panel visible) is taken.
  const outletCtx = useOutletContext<AppShellContext | undefined>();
  const isPremium = outletCtx?.isPremium ?? false;
  const isAdmin = outletCtx?.isAdmin ?? false;
  const canSeeSystemPrompt = !isPremium || isAdmin;
  // Default to enabled while the deployment config is loading so OSS users
  // (and the test harness that doesn't mock useAppConfig) see the affordance
  // immediately. Premium flips this off via CHAT_WEB_ATTACHMENTS_ENABLED=false
  // while CloudFront's body-size cap keeps uploads from reaching the worker.
  const { data: appConfig } = useAppConfig();
  const chatAttachmentsEnabled = appConfig?.chat_web_attachments_enabled ?? true;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [pendingCount, setPendingCount] = useState(0);
  const pendingRef = useRef(0);
  const sending = pendingCount > 0;
  // Separate from `pendingCount`: only counts messages whose POST already
  // landed (onAccepted fired) and that are now waiting on the agent's
  // reply. Drives the "Thinking..." spinner so it doesn't appear while the
  // user is still watching their image bytes go up. #1368.
  const [thinkingCount, setThinkingCount] = useState(0);
  const thinking = thinkingCount > 0;
  // Mirror of selectedFiles. The unmount cleanup reads this ref so it can
  // revoke chip preview URLs without resubscribing on every render (which
  // would race against removeFile's own revoke). #1368.
  const selectedFilesRef = useRef<SelectedFile[]>([]);
  const [selectedFiles, _setSelectedFiles] = useState<SelectedFile[]>([]);
  // Wrap setSelectedFiles so selectedFilesRef stays in lockstep with state.
  // A missed sync would let a staged chip URL leak past navigation.
  const setSelectedFiles: typeof _setSelectedFiles = useCallback((value) => {
    _setSelectedFiles((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      selectedFilesRef.current = next;
      return next;
    });
  }, []);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [currentTool, setCurrentTool] = useState<string | null>(null);
  const { agentBusy, activityTool, doneTick } = useChatActivity();
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
  const mountedRef = useRef(true);
  // In-flight image compressions kicked off from handleFileSelect. Awaited
  // in handleSubmit so the upload always uses the shrunken bytes, even if
  // the user hits send within the ~few-hundred-ms compression window for a
  // phone-sized photo. #1368.
  const compressionPromisesRef = useRef<Set<Promise<unknown>>>(new Set());

  // Track mounted state to prevent state updates after unmount
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Revoke any staged chip preview URLs when the user navigates away with
  // an attachment still selected (otherwise those blob URLs leak until page
  // reload). URLs already transferred to a sent message are owned by the
  // message and revoked separately on conversation refetch / failed send.
  // Uses selectedFilesRef so the cleanup sees the latest selection at unmount
  // time without resubscribing on every render.
  useEffect(() => {
    return () => {
      selectedFilesRef.current.forEach((entry) => {
        if (entry.previewUrl) URL.revokeObjectURL(entry.previewUrl);
      });
    };
  }, []);

  // Refresh conversation data when the agent finishes, so replies produced while
  // we were mounted elsewhere (e.g. user switched tabs mid-send) show up on
  // return. Skip when webchat is actively waiting for its own SSE reply:
  // handleSubmit appends the reply and invalidates queries itself, and an
  // early refetch here would race with the SSE handler and duplicate the
  // assistant message. The activity subscription itself lives in
  // ChatActivityProvider so it survives tab navigation.
  useEffect(() => {
    if (doneTick === 0) return;
    if (pendingRef.current > 0) return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.conversation.all });
  }, [doneTick, queryClient]);

  // Fetch the user's conversation. The activity SSE stream invalidates this
  // query on the "done" event, so periodic polling is unnecessary.
  const {
    data: sessionDetail,
    isPending: loadingHistory,
  } = useConversation();
  const hasConversation = !!sessionDetail && sessionDetail.session_id !== '';

  // Lazy-load the live system prompt. Only fetches once the user expands
  // the collapsible panel; refetches on every expand so the displayed
  // prompt reflects current user state (memory edits, profile changes,
  // onboarding transitions) rather than a stale snapshot.
  const {
    data: systemPromptData,
    isFetching: systemPromptFetching,
    isError: systemPromptError,
  } = useConversationSystemPrompt({ enabled: canSeeSystemPrompt && systemPromptOpen && hasConversation });

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

  // Populate messages from conversation history when it loads.
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

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newFiles = Array.from(e.target.files || []);
    if (newFiles.length === 0) {
      e.target.value = '';
      return;
    }

    const newEntries: SelectedFile[] = newFiles.map((file) => {
      const willCompress = shouldCompressImage(file);
      return {
        file,
        previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
        compressing: willCompress,
      };
    });
    setSelectedFiles((prev) => [...prev, ...newEntries]);

    // Kick off compression in the background. Each promise lands in the
    // ref so handleSubmit can await any still in flight at submit time.
    newEntries.forEach((entry) => {
      if (!entry.compressing) return;
      const original = entry.file;
      const promise = (async () => {
        const compressed = await compressImageIfNeeded(original);
        if (!mountedRef.current) return;
        setSelectedFiles((prev) =>
          prev.map((e) =>
            e.file === original ? { ...e, file: compressed, compressing: false } : e,
          ),
        );
      })();
      compressionPromisesRef.current.add(promise);
      promise.finally(() => {
        compressionPromisesRef.current.delete(promise);
      });
    });

    // Reset so the same file can be re-selected
    e.target.value = '';
  };

  const removeFile = (index: number) => {
    setSelectedFiles((prev) => {
      const entry = prev[index];
      if (entry?.previewUrl) URL.revokeObjectURL(entry.previewUrl);
      return prev.filter((_, i) => i !== index);
    });
  };

  // Core send loop, shared by handleSubmit (fresh send) and handleRetry
  // (re-attempt of a failed-upload message). When *retryMsgId* is set, the
  // existing message keeps its bubble + attachments and only the upload
  // state is reset; on a fresh send we build a new optimistic ChatMessage.
  // #1368.
  const sendChatMessage = useCallback(
    async (text: string, files: File[] | undefined, retryMsgId?: number) => {
      const hasFiles = !!files && files.length > 0;
      const abort = new AbortController();

      // -- Build or revive the optimistic user bubble ---------------------
      let msgId: number;
      if (retryMsgId !== undefined) {
        msgId = retryMsgId;
        setMessages((prev) => prev.map((m) =>
          m.id === retryMsgId
            ? {
                ...m,
                uploadState: hasFiles ? 'uploading' : undefined,
                uploadProgress: hasFiles ? 0 : undefined,
                uploadAbort: hasFiles ? abort : undefined,
              }
            : m,
        ));
      } else {
        const attachments: FileAttachment[] = (files ?? []).map((file) => ({
          name: file.name,
          type: file.type,
          // Reuse the blob URLs created at file-select time so we don't
          // allocate twice. After this call the URLs are owned by the
          // rendered message; the chip row was cleared in handleSubmit
          // without revoking.
          previewUrl: file.type.startsWith('image/')
            ? selectedFilesRef.current.find((e) => e.file === file)?.previewUrl
            : undefined,
        }));
        msgId = nextId.current++;
        const userMsg: ChatMessage = {
          id: msgId,
          role: 'user',
          body: text,
          timestamp: new Date(),
          attachments: attachments.length > 0 ? attachments : undefined,
          uploadState: hasFiles ? 'uploading' : undefined,
          uploadProgress: hasFiles ? 0 : undefined,
          uploadAbort: hasFiles ? abort : undefined,
          uploadOriginals: hasFiles ? { text, files: files! } : undefined,
        };
        setMessages((prev) => [...prev, userMsg]);
      }

      pendingRef.current++;
      setPendingCount((c) => c + 1);
      // Tracks whether onAccepted fired for this call, so the finally
      // block knows whether to decrement thinkingCount (which was only
      // incremented in onAccepted, not on send-click).
      let thinkingIncremented = false;

      try {
        const toolNames: string[] = [];
        const res = await api.sendChatMessage(
          text,
          files,
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
          () => {
            // POST succeeded with a request_id, i.e. the upload bytes are
            // safely on the server and the agent is now the bottleneck.
            // Two transitions land at this exact moment:
            //   1. Clear the upload overlay on the bubble. The previous
            //      version cleared it only after api.sendChatMessage
            //      resolved (i.e. after the agent's full reply), so the
            //      progress ring would stick at whatever value the final
            //      xhr.upload.progress event reported until the agent
            //      finished -- typically seconds to minutes. That's the
            //      "stuck at 40%" symptom from #1368.
            //   2. Flip on the "Thinking..." spinner, since the agent is
            //      now the bottleneck.
            if (!mountedRef.current) return;
            setMessages((prev) => prev.map((m) =>
              m.id === msgId
                ? {
                    ...m,
                    uploadState: undefined,
                    uploadProgress: undefined,
                    uploadAbort: undefined,
                  }
                : m,
            ));
            thinkingIncremented = true;
            setThinkingCount((c) => c + 1);
          },
          hasFiles
            ? {
                signal: abort.signal,
                onProgress: (loaded, total) => {
                  if (!mountedRef.current) return;
                  const pct = total > 0 ? loaded / total : 0;
                  setMessages((prev) => prev.map((m) =>
                    m.id === msgId ? { ...m, uploadProgress: pct } : m,
                  ));
                },
              }
            : undefined,
        );
        if (!mountedRef.current) return;
        // The upload overlay was already cleared by the onAccepted
        // callback above; we just need to drop the agent reply in.

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

        // Refresh conversation data so full tool interactions from the DB replace
        // the partial names collected from SSE events
        void queryClient.invalidateQueries({ queryKey: queryKeys.conversation.all });
      } catch (err: unknown) {
        if (!mountedRef.current) return;
        const errMsg = err instanceof Error ? err.message : 'Failed to send message';
        // A user-initiated cancel removes the bubble outright (matching
        // Telegram / WhatsApp). Other upload failures keep the bubble so the
        // user can retry without losing context. Text-only sends still drop
        // on failure since there is nothing to retry that the user can't
        // just retype.
        const wasCanceled = err instanceof Error && err.message === 'Upload canceled.';
        if (wasCanceled || !hasFiles) {
          setMessages((prev) => {
            const removed = prev.find((m) => m.id === msgId);
            removed?.attachments?.forEach((att) => {
              if (att.previewUrl) URL.revokeObjectURL(att.previewUrl);
            });
            return prev.filter((m) => m.id !== msgId);
          });
          if (!wasCanceled) toast.error(errMsg);
        } else {
          setMessages((prev) => prev.map((m) =>
            m.id === msgId
              ? { ...m, uploadState: 'failed', uploadAbort: undefined }
              : m,
          ));
          toast.error(errMsg);
        }
      } finally {
        if (!mountedRef.current) return;
        pendingRef.current--;
        setPendingCount((c) => c - 1);
        if (thinkingIncremented) {
          setThinkingCount((c) => Math.max(0, c - 1));
        }
        // Only clear indicators when all pending requests are done
        if (pendingRef.current === 0) {
          setCurrentTool(null);
          setWaitingForApproval(false);
        }
      }
    },
    [queryClient],
  );

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text && selectedFiles.length === 0) return;

    // Wait for any still-running compressions before sampling File handles,
    // so the upload always uses the compressed bytes.
    if (compressionPromisesRef.current.size > 0) {
      await Promise.all([...compressionPromisesRef.current]);
      if (!mountedRef.current) return;
    }

    const files =
      selectedFilesRef.current.length > 0
        ? selectedFilesRef.current.map((entry) => entry.file)
        : undefined;
    setInput('');
    setSelectedFiles([]);
    await sendChatMessage(text, files);
  };

  const handleCancelUpload = (msg: ChatMessage) => {
    msg.uploadAbort?.abort();
  };

  const handleRetryUpload = (msg: ChatMessage) => {
    if (!msg.uploadOriginals) return;
    void sendChatMessage(
      msg.uploadOriginals.text,
      msg.uploadOriginals.files,
      msg.id,
    );
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
    if (!msg.seq || deletingMsgId !== null) return;
    try {
      await api.deleteMessage(msg.seq);
      // API succeeded: play exit animation, then remove from state
      setDeletingMsgId(msg.id);
      await new Promise((r) => setTimeout(r, 250));
      setMessages((prev) => prev.filter((m) => m.id !== msg.id));
      setDeletingMsgId(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversation.all });
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
    if (selectedSeqs.size === 0 || isBatchDeleting) return;
    // Filter to only seqs that still exist in messages
    const validSeqs = [...selectedSeqs].filter((seq) =>
      messages.some((m) => m.seq === seq),
    );
    if (validSeqs.length === 0) return;
    setIsBatchDeleting(true);
    setConfirmModal(null);
    try {
      await api.deleteMessages(validSeqs);
      // API succeeded: play exit animation, then remove from state
      setBatchExitingSeqs(new Set(validSeqs));
      await new Promise((r) => setTimeout(r, 250));
      setMessages((prev) =>
        prev.filter((m) => !m.seq || !validSeqs.includes(m.seq)),
      );
      setBatchExitingSeqs(new Set());
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversation.all });
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
        {hasConversation && messages.length > 0 && (
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
                    if (isDeleting) return;
                    setConfirmModal({
                      title: 'Clear conversation history',
                      message: 'Delete all conversation messages? Your memory and personality will be kept.',
                      confirmLabel: 'Clear all',
                      onConfirm: async () => {
                        setConfirmModal(null);
                        setIsDeleting(true);
                        try {
                          await api.deleteConversationHistory();
                          setMessages([]);
                          void queryClient.invalidateQueries({ queryKey: queryKeys.conversation.all });
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
            {hasConversation && canSeeSystemPrompt && (
              <div className="border border-border rounded-lg overflow-hidden">
                <button
                  type="button"
                  onClick={() => setSystemPromptOpen((o) => !o)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:bg-panel transition-colors"
                >
                  <ChevronIcon open={systemPromptOpen} />
                  <span className="font-medium">Current system prompt</span>
                  {systemPromptData?.is_onboarding && (
                    <span className="ml-auto rounded bg-warning/20 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-warning-foreground">
                      Onboarding
                    </span>
                  )}
                </button>
                {systemPromptOpen && (
                  <div className="px-3 pb-3 text-xs text-muted-foreground whitespace-pre-wrap border-t border-border bg-panel/50">
                    {systemPromptFetching && !systemPromptData ? (
                      <div className="py-2">Loading current prompt…</div>
                    ) : systemPromptError ? (
                      <div className="py-2 text-destructive">
                        Failed to load the current system prompt.
                      </div>
                    ) : (
                      systemPromptData?.system_prompt
                    )}
                  </div>
                )}
              </div>
            )}
            {messages.map((msg) => {
              const isExiting = batchExitingSeqs.size > 0
                ? (msg.seq !== undefined && batchExitingSeqs.has(msg.seq))
                : deletingMsgId === msg.id;
              return (
                <div
                  key={msg.id}
                  style={isExiting ? { animation: 'message-out 250ms ease-in forwards' } : undefined}
                >
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
                      {msg.attachments.map((att, i) => {
                        const showOverlay = msg.uploadState !== undefined;
                        const dimmed = showOverlay ? 'opacity-60' : '';
                        if (att.previewUrl) {
                          return (
                            <div key={i} className="relative">
                              <img
                                src={att.previewUrl}
                                alt={att.name}
                                className={`max-w-[200px] max-h-[150px] rounded object-cover transition-opacity ${dimmed}`}
                              />
                              {showOverlay && (
                                <UploadOverlay
                                  state={msg.uploadState!}
                                  progress={msg.uploadProgress ?? 0}
                                  onCancel={() => handleCancelUpload(msg)}
                                  onRetry={() => handleRetryUpload(msg)}
                                />
                              )}
                            </div>
                          );
                        }
                        return (
                          <div key={i} className="relative">
                            <div
                              className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded transition-opacity ${
                                msg.role === 'user' ? 'bg-white/20' : 'bg-muted'
                              } ${dimmed}`}
                            >
                              <FileIcon />
                              <span className="truncate max-w-[120px]">{att.name}</span>
                            </div>
                            {showOverlay && (
                              <UploadOverlay
                                state={msg.uploadState!}
                                progress={msg.uploadProgress ?? 0}
                                onCancel={() => handleCancelUpload(msg)}
                                onRetry={() => handleRetryUpload(msg)}
                                compact
                              />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {msg.body && (
                    <p className="text-sm whitespace-pre-wrap break-words">{msg.body}</p>
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
                                <div className="font-mono text-[14px] whitespace-pre-wrap break-words max-h-60 overflow-y-auto bg-panel/50 rounded px-2 py-1.5">
                                  {result || 'No result'}
                                </div>
                                {hasArgs && (
                                  <div>
                                    <span className="text-xs font-medium opacity-70">
                                      Args
                                    </span>
                                    <pre className="font-mono text-[14px] whitespace-pre-wrap break-words max-h-40 overflow-y-auto bg-panel/50 rounded px-2 py-1.5 mt-0.5">
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

            {thinking && !waitingForApproval && (
              <ToolUseIndicator toolName={currentTool ?? undefined} />
            )}
            {!thinking && agentBusy && (
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
          {chatAttachmentsEnabled && (
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_FILE_TYPES}
              onChange={handleFileSelect}
              className="hidden"
            />
          )}
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
                {selectedFiles.map((entry, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-1.5 bg-card border border-border text-foreground text-xs px-2 py-1 rounded-md"
                  >
                    {entry.previewUrl ? (
                      <img
                        src={entry.previewUrl}
                        alt={entry.file.name}
                        className="w-5 h-5 rounded object-cover"
                      />
                    ) : (
                      <FileIcon />
                    )}
                    <span className="truncate max-w-[100px]">{entry.file.name}</span>
                    <Tooltip content={`Remove ${entry.file.name}`} delay={400} closeDelay={0}>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => removeFile(i)}
                        className="ml-0.5 text-muted-foreground hover:text-foreground"
                        aria-label={`Remove ${entry.file.name}`}
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
              {chatAttachmentsEnabled ? (
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
              ) : (
                <span />
              )}
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

/**
 * Telegram / WhatsApp-style upload status overlay shown on a sending or
 * failed attachment. While `uploading`, a circular progress ring sits at the
 * center of the image with a clickable X to cancel. On `failed`, the ring is
 * replaced by a retry icon and the user clicks anywhere on the overlay to
 * re-attempt. `compact` strips the absolute-fill positioning for the
 * non-image file chip variant where the overlay sits in the corner. #1368.
 */
function UploadOverlay({
  state,
  progress,
  onCancel,
  onRetry,
  compact = false,
}: {
  state: 'uploading' | 'failed';
  progress: number;
  onCancel: () => void;
  onRetry: () => void;
  compact?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(progress * 100)));
  if (compact) {
    return (
      <div className="absolute -top-1 -right-1">
        {state === 'uploading' ? (
          <button
            type="button"
            onClick={onCancel}
            className="w-5 h-5 rounded-full bg-black/70 text-white flex items-center justify-center hover:bg-black/85"
            aria-label="Cancel upload"
          >
            <CloseIcon />
          </button>
        ) : (
          <button
            type="button"
            onClick={onRetry}
            className="w-5 h-5 rounded-full bg-danger text-white flex items-center justify-center hover:bg-danger/85"
            aria-label="Retry upload"
          >
            <RetryIcon />
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="absolute inset-0 flex items-center justify-center rounded">
      {state === 'uploading' ? (
        <button
          type="button"
          onClick={onCancel}
          className="relative w-12 h-12 rounded-full bg-black/60 text-white flex items-center justify-center hover:bg-black/75 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
          aria-label={`Cancel upload (${pct}%)`}
        >
          <ProgressRing percent={pct} />
          <CloseIcon />
        </button>
      ) : (
        <button
          type="button"
          onClick={onRetry}
          className="px-3 py-2 rounded-full bg-danger text-white text-xs font-medium flex items-center gap-1.5 hover:bg-danger/85 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
          aria-label="Retry upload"
        >
          <RetryIcon />
          <span>Retry</span>
        </button>
      )}
    </div>
  );
}

/** Concentric SVG ring whose stroke length tracks `percent` (0-100). */
function ProgressRing({ percent }: { percent: number }) {
  const r = 20;
  const c = 2 * Math.PI * r;
  const offset = c - (Math.max(0, Math.min(100, percent)) / 100) * c;
  return (
    <svg
      className="absolute inset-0 -rotate-90"
      width="100%"
      height="100%"
      viewBox="0 0 48 48"
      aria-hidden="true"
    >
      <circle
        cx="24"
        cy="24"
        r={r}
        fill="none"
        stroke="rgba(255,255,255,0.25)"
        strokeWidth="3"
      />
      <circle
        cx="24"
        cy="24"
        r={r}
        fill="none"
        stroke="white"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={offset}
        style={{ transition: 'stroke-dashoffset 120ms linear' }}
      />
    </svg>
  );
}

function RetryIcon() {
  return (
    <svg
      className="w-3.5 h-3.5"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M4 4v6h6M20 20v-6h-6M5.5 9A7 7 0 0119 9M18.5 15A7 7 0 015 15"
      />
    </svg>
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

