import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Card from '@/components/ui/card';
import Badge from '@/components/ui/badge';
import Button from '@/components/ui/button';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import type { SessionSummary, SessionDetail, SessionMessage } from '@/types';

/** Map internal channel identifiers to user-friendly labels. */
function channelLabel(channel: string): string {
  switch (channel) {
    case 'telegram': return 'Telegram';
    case 'webchat': return 'Web Chat';
    default: return channel;
  }
}

export default function ConversationsPage() {
  const { sessionId } = useParams<{ sessionId: string }>();

  if (sessionId) {
    return <SessionDetailView sessionId={sessionId} />;
  }
  return <SessionListView />;
}

// --- Session List ---

const PAGE_SIZE = 20;

function SessionListView() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loaderRef = useRef<HTMLDivElement | null>(null);

  const hasMore = sessions.length < total;

  // Initial load
  const loadInitial = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listSessions(0, PAGE_SIZE)
      .then((res) => {
        setSessions(res.sessions);
        setTotal(res.total);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadInitial(); }, [loadInitial]);

  // Load next page (append to existing sessions)
  const loadMore = useCallback(() => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    api.listSessions(sessions.length, PAGE_SIZE)
      .then((res) => {
        setSessions((prev) => [...prev, ...res.sessions]);
        setTotal(res.total);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoadingMore(false));
  }, [loadingMore, hasMore, sessions.length]);

  // IntersectionObserver triggers loadMore when sentinel is visible
  useEffect(() => {
    const node = loaderRef.current;
    if (!node || !hasMore || loadingMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadMore();
      },
      { rootMargin: '200px' },
    );
    observer.observe(node);
    return () => { observer.disconnect(); };
  }, [hasMore, loadingMore, loadMore]);

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold">Conversations</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Review past conversations with your AI assistant.
        </p>
      </div>

      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : error ? (
        <Card className="text-center py-8">
          <p className="text-sm text-danger">{error}</p>
          <Button variant="secondary" size="sm" className="mt-2" onClick={() => loadInitial()}>
            Retry
          </Button>
        </Card>
      ) : sessions.length === 0 ? (
        <Card className="text-center py-8">
          <p className="text-sm text-muted-foreground">
            No conversations yet. Start chatting via the Chat page or Telegram!
          </p>
        </Card>
      ) : (
        <>
          <p className="text-sm text-muted-foreground mb-3">
            {total} conversation{total !== 1 ? 's' : ''}
          </p>

          <div className="space-y-2">
            {sessions.map((s) => (
              <Card
                key={s.id}
                className="cursor-pointer hover:border-primary/50 transition-colors"
                onClick={() => navigate(`/app/conversations/${s.id}`)}
              >
                <div className="flex items-center justify-between">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium truncate">
                      {s.last_message_preview || 'No messages'}
                    </p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {new Date(s.start_time).toLocaleString()}
                    </p>
                  </div>
                  <div className="ml-3 flex items-center gap-2 shrink-0">
                    {s.channel && <Badge variant="outline">{channelLabel(s.channel)}</Badge>}
                    <Badge>{s.message_count} msgs</Badge>
                  </div>
                </div>
              </Card>
            ))}
          </div>

          {/* Infinite scroll sentinel */}
          <div
            ref={loaderRef}
            className="flex justify-center py-4"
            data-testid="scroll-sentinel"
          >
            {loadingMore && <Spinner />}
            {!hasMore && sessions.length > PAGE_SIZE && (
              <p className="text-xs text-muted-foreground">All conversations loaded</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// --- Session Detail ---

function SessionDetailView({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api.getSession(sessionId)
      .then(setSession)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sessionId]);

  return (
    <div>
      <button
        onClick={() => navigate('/app/conversations')}
        className="text-sm text-primary hover:underline mb-4 inline-flex items-center gap-1"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
        Back to conversations
      </button>

      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : error ? (
        <Card className="text-center py-8">
          <p className="text-sm text-danger">{error}</p>
        </Card>
      ) : session ? (
        <>
          <div className="mb-4 flex items-center gap-3">
            <h2 className="text-xl font-semibold">Conversation</h2>
            {session.channel && <Badge variant="outline">{channelLabel(session.channel)}</Badge>}
            {session.is_active && <Badge>Active</Badge>}
            <Button
              variant="secondary"
              size="sm"
              className="ml-auto"
              onClick={() => navigate(`/app/chat?session=${session.session_id}`)}
            >
              Resume in Chat
            </Button>
          </div>
          <p className="text-xs text-muted-foreground mb-4">
            Started: {new Date(session.created_at).toLocaleString()}
            {' | '}
            Last message: {new Date(session.last_message_at).toLocaleString()}
          </p>

          <div className="space-y-3">
            {session.messages.map((msg) => (
              <MessageBubble key={msg.seq} message={msg} />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

function MessageBubble({ message }: { message: SessionMessage }) {
  const isUser = message.direction === 'inbound';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[80%] rounded-[--radius-lg] px-4 py-2.5 ${
          isUser
            ? 'bg-primary text-white'
            : 'bg-card border border-border'
        }`}
      >
        <p className="text-sm whitespace-pre-wrap">{message.body}</p>

        {message.tool_interactions.length > 0 && (
          <div className="mt-2 space-y-1">
            {message.tool_interactions.map((tool, i) => (
              <div
                key={i}
                className={`text-xs px-2 py-1 rounded-[--radius-sm] ${
                  isUser
                    ? 'bg-white/10'
                    : 'bg-panel'
                }`}
              >
                <span className="font-medium">Tool: </span>
                {String(tool['name'] ?? tool['tool'] ?? 'unknown')}
                {'result' in tool && (
                  <span className="opacity-70"> - {String(tool['result']).slice(0, 100)}</span>
                )}
              </div>
            ))}
          </div>
        )}

        <p className={`text-[10px] mt-1 ${isUser ? 'text-white/60' : 'text-muted-foreground'}`}>
          {new Date(message.timestamp).toLocaleTimeString()}
        </p>
      </div>
    </div>
  );
}
