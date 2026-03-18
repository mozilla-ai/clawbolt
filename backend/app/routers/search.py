"""Unified search endpoint across sessions and memory facts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.session_db import get_session_store
from backend.app.auth.dependencies import get_current_user
from backend.app.models import User

router = APIRouter()


class SearchResult(BaseModel):
    type: str
    title: str
    preview: str
    url: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    query: str


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query("", min_length=0, max_length=200),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """Search across conversations and memory facts."""
    query = q.strip().lower()
    if not query:
        return SearchResponse(results=[], query=q)

    results: list[SearchResult] = []

    # Search memory (line-by-line text search over freeform MEMORY.md)
    memory_store = get_memory_store(current_user.id)
    memory_text = memory_store.read_memory()
    for line in memory_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and query in stripped.lower():
            results.append(
                SearchResult(
                    type="memory",
                    title=stripped[:60],
                    preview=stripped[:120],
                    url="/app/memory",
                )
            )

    # Search session messages (by body content)
    session_store = get_session_store(current_user.id)
    session_ids = session_store.list_session_ids()
    for session_id in reversed(session_ids[-50:]):
        session = session_store.load_session(session_id)
        if session is None:
            continue
        for msg in session.messages:
            if query in msg.body.lower():
                results.append(
                    SearchResult(
                        type="conversation",
                        title=f"Conversation {session_id}",
                        preview=msg.body[:120],
                        url=f"/app/conversations/{session_id}",
                    )
                )
                break  # One result per session

    return SearchResponse(results=results[:20], query=q)
