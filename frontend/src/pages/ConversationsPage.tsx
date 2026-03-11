import { useState, useEffect, useCallback } from 'react';
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

function SessionListView() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const limit = 20;

  const load = useCallback((off: number) => {
    setLoading(true);
    setError(null);
    api.listSessions(off, limit)
      .then((res) => {
        setSessions(res.sessions);
        setTotal(res.total);
        setOffset(res.offset);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(0); }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;

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
          <Button variant="secondary" size="sm" className="mt-2" onClick={() => load(offset)}>Retry</Button>
        </Card>
      ) : sessions.length === 0 ? (
        <Card className="text-center py-8">
          <p className="text-sm text-muted-foreground">No conversations yet. Start chatting via the Chat page or Telegram!</p>
        </Card>
      ) : (
        <>
          <div className="space-y-2">
            {sessions.map((s) => (
              <Card
                key={s.id}
                className="group cursor-pointer hover:border-primary/50 transition-colors duration-150"
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
                  <div className="ml-3 flex items-center gap-2 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
                    {s.channel && <Badge variant="outline">{channelLabel(s.channel)}</Badge>}
                    <Badge>{s.message_count} msgs</Badge>
                  </div>
                </div>
              </Card>
            ))}
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4 text-sm text-muted-foreground">
              <span>{total} conversation{total !== 1 ? 's' : ''}</span>
              <div className="flex items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => load(offset - limit)}
                >
                  Previous
                </Button>
                <span>Page {currentPage} of {totalPages}</span>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={currentPage >= totalPages}
                  onClick={() => load(offset + limit)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
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
        className="text-sm text-primary hover:underline mb-4 inline-flex items-center gap-1 transition-colors duration-150"
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
