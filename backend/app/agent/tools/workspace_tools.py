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
_ALLOWED_EXTENSIONS = {".md", ".json"}

# Files that cannot be deleted by the agent.
_PROTECTED_FILES = {
    "USER.md",
    "SOUL.md",
    "HEARTBEAT.md",
    "MEMORY.md",
    "HISTORY.md",
    "PERMISSIONS.json",
}

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

# Files routed to the UserPermissionSet table instead of disk. The
# ApprovalStore uses the same row, so read_file/write_file/edit_file and
# the approval gate share a single source of truth.
_PERMISSIONS_FILE = "PERMISSIONS.json"


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


def _extract_path(args: dict[str, object]) -> str | None:
    """Extract the file path from workspace tool arguments."""
    return str(args["path"]) if args.get("path") else None


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
            f"Only markdown (.md) and JSON (.json) files are supported, got: {resolved.suffix or '(none)'}",
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


def _is_permissions_path(relative_path: str) -> bool:
    """Return True if ``relative_path`` refers to the top-level PERMISSIONS.json."""
    try:
        name = Path(relative_path).name
    except (ValueError, OSError):
        return False
    if name != _PERMISSIONS_FILE:
        return False
    stripped = relative_path.lstrip("./")
    return not ("/" in stripped or relative_path.startswith(".."))


def _permissions_read_sync(user_id: str) -> str:
    from backend.app.models import UserPermissionSet

    db = SessionLocal()
    try:
        row = db.query(UserPermissionSet).filter_by(user_id=user_id).first()
        if row is None:
            return ""
        return row.data or ""
    finally:
        db.close()


async def _permissions_read(user_id: str) -> str:
    return await asyncio.to_thread(_permissions_read_sync, user_id)


def _permissions_write_sync(user_id: str, content: str) -> None:
    """Persist PERMISSIONS.json content, normalizing to indented JSON.

    Stable pretty-printing matters: the approval store pretty-prints on
    every set_permission, and edit_file old_text matching breaks if one
    writer leaves minified JSON around. We parse + re-serialize so every
    reader sees the same shape regardless of what the caller passed in.
    Malformed JSON is stored verbatim so a user who's mid-edit can
    recover, but this is the narrow escape hatch, not the hot path.
    """
    import json as _json

    from backend.app.database import db_session
    from backend.app.models import UserPermissionSet

    try:
        parsed = _json.loads(content)
    except (_json.JSONDecodeError, ValueError):
        payload = content
    else:
        payload = _json.dumps(parsed, indent=2, default=str)

    with db_session() as db:
        row = db.query(UserPermissionSet).filter_by(user_id=user_id).first()
        if row is None:
            db.add(UserPermissionSet(user_id=user_id, data=payload))
        else:
            row.data = payload
        db.commit()


async def _permissions_write(user_id: str, content: str) -> None:
    await asyncio.to_thread(_permissions_write_sync, user_id, content)


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

        if _is_permissions_path(path):
            content = await _permissions_read(user_id)
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

        if _is_permissions_path(path):
            await _permissions_write(user_id, content)
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

        if _is_permissions_path(path):
            text = await _permissions_read(user_id)
            if old_text not in text:
                return ToolResult(
                    content=f"Text not found in {path}. Read the file first to see current contents.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            count = text.count(old_text)
            if count > 1:
                return ToolResult(
                    content=(
                        f"Found {count} matches in {path}. Provide more context to match uniquely."
                    ),
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
            updated = text.replace(old_text, new_text, 1)
            await _permissions_write(user_id, updated)
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
                "Read a markdown or JSON file from your workspace. "
                "Use to check USER.md, SOUL.md, memory files, or PERMISSIONS.json."
            ),
            function=read_file,
            params_model=ReadFileParams,
            usage_hint=(
                "Read USER.md to see what you know about the user. "
                "Read SOUL.md to check your personality. "
                "Read memory/MEMORY.md to review long-term facts. "
                "Read PERMISSIONS.json to see current tool permission levels."
            ),
        ),
        Tool(
            name=ToolName.WRITE_FILE,
            description=(
                "Write or overwrite a markdown or JSON file in your workspace. "
                "Use to update USER.md with user info, SOUL.md with your personality, "
                "or PERMISSIONS.json to reset permissions."
            ),
            function=write_file,
            params_model=WriteFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            usage_hint=(
                "Write to USER.md when you learn about the user (rates, hours, preferences, etc.). "
                "Write to SOUL.md when the user defines your personality. "
                "Write to PERMISSIONS.json to reset all permissions to defaults."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                resource_extractor=_extract_path,
                description_builder=lambda args: f"Write to {args.get('path', 'file')}",
            ),
        ),
        Tool(
            name=ToolName.EDIT_FILE,
            description=(
                "Replace exact text in a markdown or JSON file. "
                "Use for targeted updates to USER.md, SOUL.md, PERMISSIONS.json, etc. "
                "Read the file first to see current contents."
            ),
            function=edit_file,
            params_model=EditFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                resource_extractor=_extract_path,
                description_builder=lambda args: f"Edit {args.get('path', 'file')}",
            ),
        ),
        Tool(
            name=ToolName.DELETE_FILE,
            description=(
                "Delete a file from your workspace. "
                "Cannot delete protected files (USER.md, SOUL.md, HEARTBEAT.md, PERMISSIONS.json)."
            ),
            function=delete_file,
            params_model=DeleteFileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_path,
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
            SubToolInfo(ToolName.READ_FILE, "Read markdown and JSON files from workspace"),
            SubToolInfo(ToolName.WRITE_FILE, "Write or overwrite markdown and JSON files"),
            SubToolInfo(ToolName.EDIT_FILE, "Replace text in markdown and JSON files"),
            SubToolInfo(
                ToolName.DELETE_FILE, "Delete files from workspace", default_permission="ask"
            ),
        ],
    )


_register()
