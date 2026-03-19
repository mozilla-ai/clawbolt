"""Generic file tools for reading and writing workspace files.

Gives the agent direct access to markdown files in the user's data
directory (SOUL.md, USER.md, memory/, etc.), following the same pattern
used by openclaw and nanobot.

USER.md, SOUL.md, and HEARTBEAT.md are stored as columns on the User
DB row and presented to the agent as virtual files. MEMORY.md and
HISTORY.md are stored in the MemoryDocument table. All other .md files
(BOOTSTRAP.md, etc.) are stored on disk.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult, ToolTags
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import MemoryDocument, User

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Only allow markdown files to be read/written by the agent.
_ALLOWED_EXTENSIONS = {".md"}

# Files that cannot be deleted by the agent.
_PROTECTED_FILES = {"USER.md", "SOUL.md", "HEARTBEAT.md", "MEMORY.md", "HISTORY.md"}

# Files stored as DB columns on User rather than on disk.
_DB_FILE_COLUMN: dict[str, str] = {
    "USER.md": "user_text",
    "SOUL.md": "soul_text",
    "HEARTBEAT.md": "heartbeat_text",
}

# Files stored in the MemoryDocument table (not User table).
_MEMORY_DOC_COLUMN: dict[str, str] = {
    "MEMORY.md": "memory_text",
    "HISTORY.md": "history_text",
}


class ReadFileParams(BaseModel):
    """Parameters for the read_file tool."""

    path: str = Field(
        description="Relative path within your workspace (e.g. 'USER.md', 'memory/MEMORY.md')"
    )


class WriteFileParams(BaseModel):
    """Parameters for the write_file tool."""

    path: str = Field(description="Relative path within your workspace (e.g. 'USER.md', 'SOUL.md')")
    content: str = Field(description="Full file content to write")


class EditFileParams(BaseModel):
    """Parameters for the edit_file tool."""

    path: str = Field(description="Relative path within your workspace (e.g. 'USER.md')")
    old_text: str = Field(description="Exact text to find and replace")
    new_text: str = Field(description="Replacement text")


class DeleteFileParams(BaseModel):
    """Parameters for the delete_file tool."""

    path: str = Field(description="Relative path within your workspace (e.g. 'BOOTSTRAP.md')")


def _resolve_path(user_id: str, relative_path: str) -> tuple[Path, str | None]:
    """Resolve a relative path to an absolute path within the user directory.

    Returns (resolved_path, error_message).  error_message is None on success.
    """
    base = Path(settings.data_dir) / str(user_id)
    try:
        resolved = (base / relative_path).resolve()
    except (ValueError, OSError):
        return base, f"Invalid path: {relative_path}"

    # Prevent path traversal
    if not resolved.is_relative_to(base.resolve()):
        return base, f"Path escapes workspace: {relative_path}"

    # Only allow markdown files
    if resolved.suffix not in _ALLOWED_EXTENSIONS:
        return (
            resolved,
            f"Only markdown (.md) files are supported, got: {resolved.suffix or '(none)'}",
        )

    return resolved, None


def _db_read_sync(user_id: str, column: str) -> str:
    """Read a DB-backed virtual file column (synchronous)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).first()
        if user is None:
            return ""
        return getattr(user, column, "") or ""
    finally:
        db.close()


async def _db_read(user_id: str, column: str) -> str:
    """Read a DB-backed virtual file column."""
    return await asyncio.to_thread(_db_read_sync, user_id, column)


def _db_write_sync(user_id: str, column: str, content: str) -> None:
    """Write a DB-backed virtual file column (synchronous)."""
    from backend.app.database import db_session

    with db_session() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if user is not None:
            setattr(user, column, content)
            db.commit()


async def _db_write(user_id: str, column: str, content: str) -> None:
    """Write a DB-backed virtual file column."""
    await asyncio.to_thread(_db_write_sync, user_id, column, content)


def _canonical_name(relative_path: str) -> str | None:
    """Return the canonical filename if this path refers to a DB-backed file."""
    # Normalize paths like "./USER.md" or "USER.md" to just "USER.md"
    try:
        name = Path(relative_path).name
    except (ValueError, OSError):
        return None
    # Only match top-level files (not memory/USER.md, ../USER.md, etc.)
    stripped = relative_path.lstrip("./")
    if ("/" in stripped or relative_path.startswith("..")) and name in _DB_FILE_COLUMN:
        return None
    return name if name in _DB_FILE_COLUMN else None


def _memory_doc_column(relative_path: str) -> str | None:
    """Return the MemoryDocument column name if this path refers to a memory file.

    Recognizes: "MEMORY.md", "memory/MEMORY.md", "./memory/MEMORY.md", etc.
    """
    try:
        name = Path(relative_path).name
    except (ValueError, OSError):
        return None
    if name not in _MEMORY_DOC_COLUMN:
        return None
    stripped = relative_path.lstrip("./")
    if "/" in stripped:
        parent = str(Path(stripped).parent)
        if parent != "memory":
            return None
    return _MEMORY_DOC_COLUMN[name]


def _memory_doc_read_sync(user_id: str, column: str) -> str:
    """Read a MemoryDocument column (synchronous)."""
    db = SessionLocal()
    try:
        doc = db.query(MemoryDocument).filter_by(user_id=user_id).first()
        if doc is None:
            return ""
        return getattr(doc, column, "") or ""
    finally:
        db.close()


async def _memory_doc_read(user_id: str, column: str) -> str:
    """Read a MemoryDocument column."""
    return await asyncio.to_thread(_memory_doc_read_sync, user_id, column)


def _memory_doc_write_sync(user_id: str, column: str, content: str) -> None:
    """Write a MemoryDocument column (synchronous)."""
    from backend.app.database import db_session

    with db_session() as db:
        doc = db.query(MemoryDocument).filter_by(user_id=user_id).first()
        if doc is None:
            doc = MemoryDocument(user_id=user_id, memory_text="", history_text="")
            db.add(doc)
            db.flush()
        setattr(doc, column, content)
        db.commit()


async def _memory_doc_write(user_id: str, column: str, content: str) -> None:
    """Write a MemoryDocument column."""
    await asyncio.to_thread(_memory_doc_write_sync, user_id, column, content)


def create_workspace_tools(user_id: str) -> list[Tool]:
    """Create generic file tools scoped to the user's data directory."""

    async def read_file(path: str) -> ToolResult:
        """Read a markdown file from the workspace."""
        canon = _canonical_name(path)
        if canon:
            content = await _db_read(user_id, _DB_FILE_COLUMN[canon])
            return ToolResult(content=content or "(empty)")

        mem_col = _memory_doc_column(path)
        if mem_col:
            content = await _memory_doc_read(user_id, mem_col)
            return ToolResult(content=content or "(empty)")

        resolved, err = _resolve_path(user_id, path)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)
        if not resolved.exists():
            return ToolResult(
                content=f"File not found: {path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        file_content = await asyncio.to_thread(resolved.read_text, "utf-8")
        return ToolResult(content=file_content)

    async def write_file(path: str, content: str) -> ToolResult:
        """Write or overwrite a markdown file in the workspace."""
        canon = _canonical_name(path)
        if canon:
            await _db_write(user_id, _DB_FILE_COLUMN[canon], content)
            return ToolResult(content=f"Wrote {path}")

        mem_col = _memory_doc_column(path)
        if mem_col:
            await _memory_doc_write(user_id, mem_col, content)
            return ToolResult(content=f"Wrote {path}")

        resolved, err = _resolve_path(user_id, path)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_text, content, "utf-8")
        return ToolResult(content=f"Wrote {path}")

    async def edit_file(path: str, old_text: str, new_text: str) -> ToolResult:
        """Replace exact text in a markdown file."""
        canon = _canonical_name(path)
        if canon:
            column = _DB_FILE_COLUMN[canon]
            text = await _db_read(user_id, column)
            if old_text not in text:
                return ToolResult(
                    content=f"Text not found in {path}. Read the file first to see current contents.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            count = text.count(old_text)
            if count > 1:
                return ToolResult(
                    content=f"Found {count} matches in {path}. Provide more context to match uniquely.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
            updated = text.replace(old_text, new_text, 1)
            await _db_write(user_id, column, updated)
            return ToolResult(content=f"Updated {path}")

        mem_col = _memory_doc_column(path)
        if mem_col:
            text = await _memory_doc_read(user_id, mem_col)
            if old_text not in text:
                return ToolResult(
                    content=f"Text not found in {path}. Read the file first to see current contents.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            count = text.count(old_text)
            if count > 1:
                return ToolResult(
                    content=f"Found {count} matches in {path}. Provide more context to match uniquely.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
            updated = text.replace(old_text, new_text, 1)
            await _memory_doc_write(user_id, mem_col, updated)
            return ToolResult(content=f"Updated {path}")

        resolved, err = _resolve_path(user_id, path)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)
        if not resolved.exists():
            return ToolResult(
                content=f"File not found: {path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        text = await asyncio.to_thread(resolved.read_text, "utf-8")
        if old_text not in text:
            return ToolResult(
                content=f"Text not found in {path}. Read the file first to see current contents.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        count = text.count(old_text)
        if count > 1:
            return ToolResult(
                content=f"Found {count} matches in {path}. Provide more context to match uniquely.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        updated = text.replace(old_text, new_text, 1)
        await asyncio.to_thread(resolved.write_text, updated, "utf-8")
        return ToolResult(content=f"Updated {path}")

    async def delete_file(path: str) -> ToolResult:
        """Delete a markdown file from the workspace."""
        resolved, err = _resolve_path(user_id, path)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)
        # Check protected files after resolving to prevent bypass via ./USER.md
        if resolved.name in _PROTECTED_FILES:
            return ToolResult(
                content=f"Cannot delete protected file: {path}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        if not resolved.exists():
            return ToolResult(
                content=f"File not found: {path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        await asyncio.to_thread(resolved.unlink)
        return ToolResult(content=f"Deleted {path}")

    return [
        Tool(
            name=ToolName.READ_FILE,
            description=(
                "Read a markdown file from your workspace. "
                "Use to check USER.md, SOUL.md, or memory files."
            ),
            function=read_file,
            params_model=ReadFileParams,
            usage_hint=(
                "Read USER.md to see what you know about the user. "
                "Read SOUL.md to check your personality. "
                "Read memory/MEMORY.md to review long-term facts."
            ),
        ),
        Tool(
            name=ToolName.WRITE_FILE,
            description=(
                "Write or overwrite a markdown file in your workspace. "
                "Use to update USER.md with user info, SOUL.md with your personality, etc."
            ),
            function=write_file,
            params_model=WriteFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            usage_hint=(
                "Write to USER.md when you learn about the user (rates, hours, preferences, etc.). "
                "Write to SOUL.md when the user defines your personality."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
                description_builder=lambda args: f"Write to {args.get('path', 'file')}",
            ),
        ),
        Tool(
            name=ToolName.EDIT_FILE,
            description=(
                "Replace exact text in a markdown file. "
                "Use for targeted updates to USER.md, SOUL.md, etc. "
                "Read the file first to see current contents."
            ),
            function=edit_file,
            params_model=EditFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
                description_builder=lambda args: f"Edit {args.get('path', 'file')}",
            ),
        ),
        Tool(
            name=ToolName.DELETE_FILE,
            description=(
                "Delete a markdown file from your workspace. "
                "Cannot delete protected files (USER.md, SOUL.md, HEARTBEAT.md)."
            ),
            function=delete_file,
            params_model=DeleteFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: f"Delete {args.get('path', 'file')}",
            ),
        ),
    ]


def _workspace_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for workspace tools, used by the registry."""
    return create_workspace_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "workspace",
        _workspace_factory,
        sub_tools=[
            SubToolInfo(ToolName.READ_FILE, "Read markdown files from workspace"),
            SubToolInfo(ToolName.WRITE_FILE, "Write or overwrite markdown files"),
            SubToolInfo(ToolName.EDIT_FILE, "Replace text in markdown files"),
            SubToolInfo(ToolName.DELETE_FILE, "Delete markdown files"),
        ],
    )


_register()
