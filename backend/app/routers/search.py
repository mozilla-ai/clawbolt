"""Unified search endpoint across sessions, memory facts, and clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.app.agent.file_store import (
    ClientStore,
    UserData,
    get_memory_store,
    get_session_store,
)
from backend.app.auth.dependencies import get_current_user

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
    current_user: UserData = Depends(get_current_user),
) -> SearchResponse:
    """Search across conversations, memory facts, and client records."""
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

    # Search client records (by name, email, phone, notes)
    client_store = ClientStore(user_id=current_user.id)
    clients = await client_store.list_all()
    for client in clients:
        searchable = f"{client.name} {client.email} {client.phone} {client.notes}".lower()
        if query in searchable:
            results.append(
                SearchResult(
                    type="client",
                    title=client.name or client.id,
                    preview=", ".join(
                        part for part in [client.phone, client.email, client.address] if part
                    )[:120],
                    url="/app/memory",
                )
            )

    # Search session messages (by body content)
    session_store = get_session_store(current_user.id)
    session_files = session_store._list_session_files()
    for path in reversed(session_files[-50:]):
        session_id = path.stem
        session = session_store._load_session(session_id)
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
