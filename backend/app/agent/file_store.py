"""File-based storage layer replacing PostgreSQL/SQLAlchemy.

All file I/O goes through this module. Stores use JSON, JSONL, and Markdown
formats following patterns from nanobot (JSONL sessions, MEMORY.md facts,
per-user directories, asyncio.Lock) and openclaw (SOUL.md for personality,
category-organized markdown memory, user_index for routing).

Storage layout::

    data/
      user_index.json
      seen_messages.json
      users/
        {user_id}/
          user.json
          SOUL.md
          USER.md
          HEARTBEAT.md
          memory/
            MEMORY.md
            HISTORY.md
          sessions/
            {session_id}.jsonl
          clients.json
          estimates/
            {client_slug}/
              EST-0001.json
          media.json
          heartbeat/
            log.jsonl
          llm_usage.jsonl
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from backend.app.agent.prompts import load_prompt
from backend.app.config import settings
from backend.app.enums import ChecklistSchedule, ChecklistStatus, EstimateStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes (replace ORM models)
# ---------------------------------------------------------------------------


class UserData(BaseModel):
    """Replaces the User ORM model."""

    id: int = 0
    user_id: str = ""
    name: str = ""
    phone: str = ""
    soul_text: str = ""
    user_text: str = ""
    checklist_text: str = ""
    timezone: str = ""
    preferred_channel: str = "telegram"
    channel_identifier: str = ""
    assistant_name: str = "Clawbolt"
    onboarding_complete: bool = False
    is_active: bool = True
    role: str = "user"
    preferences_json: str = "{}"
    heartbeat_opt_in: bool = True
    heartbeat_frequency: str = ""
    folder_scheme: str = "by_client"
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    updated_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


class StoredMessage(BaseModel):
    """Replaces the Message ORM model. One line in a session JSONL file."""

    direction: str = ""
    body: str = ""
    processed_context: str = ""
    tool_interactions_json: str = ""
    external_message_id: str = ""
    media_urls_json: str = "[]"
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    seq: int = 0


class SessionMetadata(BaseModel):
    """First line of a session JSONL file.

    Note: the ``_type`` discriminator is handled as a raw dict key when
    serializing/deserializing JSONL, not through this model.
    """

    session_id: str = ""
    user_id: int = 0
    last_message_at: str = ""
    is_active: bool = True
    last_compacted_seq: int = 0
    channel: str = ""


class SessionState(BaseModel):
    """In-memory representation of a conversation session."""

    session_id: str = ""
    user_id: int = 0
    messages: list[StoredMessage] = Field(default_factory=list)
    is_active: bool = True
    created_at: str = ""
    last_message_at: str = ""
    last_compacted_seq: int = 0
    channel: str = ""


class ClientData(BaseModel):
    """Replaces the Client ORM model."""

    id: str = ""
    name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class EstimateLineItemData(BaseModel):
    """Replaces the EstimateLineItem ORM model."""

    id: int = 0
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0


class EstimateData(BaseModel):
    """Replaces the Estimate + EstimateLineItem ORM models."""

    id: str = ""
    user_id: int = 0
    client_id: str | None = None
    description: str = ""
    total_amount: float = 0.0
    status: str = EstimateStatus.DRAFT
    pdf_url: str = ""
    storage_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    line_items: list[EstimateLineItemData] = Field(default_factory=list)


class MediaData(BaseModel):
    """Replaces the MediaFile ORM model."""

    id: str = ""
    message_id: int | None = None
    user_id: int = 0
    original_url: str = ""
    mime_type: str = ""
    processed_text: str = ""
    storage_url: str = ""
    storage_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class ChecklistItem(BaseModel):
    """Replaces the HeartbeatChecklistItem ORM model."""

    id: int = 0
    user_id: int = 0
    description: str = ""
    schedule: str = ChecklistSchedule.DAILY
    active_hours: str = ""
    last_triggered_at: str | None = None
    status: str = ChecklistStatus.ACTIVE
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class HeartbeatLogEntry(BaseModel):
    """Replaces the HeartbeatLog ORM model."""

    user_id: int = 0
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class MemoryFact(BaseModel):
    """A single fact in MEMORY.md."""

    key: str = ""
    value: str = ""
    category: str = "general"
    confidence: float = 1.0


class ToolConfigEntry(BaseModel):
    """A single tool group entry in a user's tool_config.json."""

    name: str = ""
    description: str = ""
    category: str = "domain"
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool = True


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any = None) -> Any:
    """Read and parse a JSON file. Returns default if file doesn't exist or is corrupt."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Corrupt JSON file %s, returning default", path)
        return default


def _write_json(path: Path, data: Any) -> None:
    """Write data as JSON to a file atomically, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.rename(path)


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    """Append a single JSON line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all lines from a JSONL file."""
    if not path.exists():
        return []
    lines: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if line:
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping invalid JSONL line in %s", path)
    return lines


def _data_dir() -> Path:
    """Return the root data directory."""
    return Path(settings.data_dir)


def _user_dir(user_id: int) -> Path:
    """Return the directory for a specific user."""
    return _data_dir() / str(user_id)


def _index_path() -> Path:
    """Return the path to user_index.json."""
    return _data_dir().parent / "user_index.json"


def _next_id(items: list[dict[str, Any]]) -> int:
    """Get the next auto-increment ID from a list of records."""
    if not items:
        return 1
    return max(item.get("id", 0) for item in items) + 1


def slugify(text: str, max_length: int = 60) -> str:
    """Convert text to a filesystem-safe slug.

    Example: "John Smith - 116 Virginia Ave" -> "john_smith_116_virginia_ave"
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:max_length].rstrip("_")


def _unique_slug(base_slug: str, existing_ids: set[str]) -> str:
    """Return a unique slug by appending a counter suffix if needed."""
    if base_slug not in existing_ids:
        return base_slug
    counter = 2
    while f"{base_slug}_{counter}" in existing_ids:
        counter += 1
    return f"{base_slug}_{counter}"


def make_client_slug(
    name: str = "",
    address: str = "",
    folder_scheme: str = "by_client",
) -> str:
    """Build a client slug based on the folder scheme preference.

    folder_scheme options:
        "by_client" (default): slug from client name
        "by_address": slug from address
        "by_client_and_address": slug from "name address"
    """
    if folder_scheme == "by_address" and address.strip():
        return slugify(address)
    if folder_scheme == "by_client_and_address":
        parts = []
        if name.strip():
            parts.append(name.strip())
        if address.strip():
            parts.append(address.strip())
        if parts:
            return slugify(" ".join(parts))
    # Default: by_client, or fallback when preferred field is empty
    if name.strip():
        return slugify(name)
    if address.strip():
        return slugify(address)
    return ""


# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------


class UserStore:
    """File-based user storage. Replaces User model + queries."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _all_dirs(self) -> list[Path]:
        """List all user directories."""
        base = _data_dir()
        if not base.exists():
            return []
        return [d for d in sorted(base.iterdir()) if d.is_dir() and d.name.isdigit()]

    def _load(self, user_id: int) -> UserData | None:
        """Load a user from disk."""
        path = _user_dir(user_id) / "user.json"
        data = _read_json(path)
        if data is None:
            return None
        user = UserData.model_validate(data)
        # Load soul_text from SOUL.md
        soul_path = _user_dir(user_id) / "SOUL.md"
        if soul_path.exists():
            raw = soul_path.read_text(encoding="utf-8").strip()
            if raw.startswith("# Soul"):
                raw = raw[len("# Soul") :].strip()
            user.soul_text = raw
        # Load user_text from USER.md
        user_path = _user_dir(user_id) / "USER.md"
        if user_path.exists():
            raw = user_path.read_text(encoding="utf-8").strip()
            if raw.startswith("# User"):
                raw = raw[len("# User") :].strip()
            user.user_text = raw
        # Load checklist_text from HEARTBEAT.md
        checklist_path = _user_dir(user_id) / "HEARTBEAT.md"
        if checklist_path.exists():
            raw = checklist_path.read_text(encoding="utf-8").strip()
            if raw.startswith("# Checklist"):
                raw = raw[len("# Checklist") :].strip()
            user.checklist_text = raw
        return user

    def _save(self, user: UserData) -> None:
        """Save a user to disk."""
        cdir = _user_dir(user.id)
        cdir.mkdir(parents=True, exist_ok=True)

        # Save user.json (exclude text fields that go to .md files)
        data = user.model_dump()
        soul_text = data.pop("soul_text", "")
        user_text = data.pop("user_text", "")
        checklist_text = data.pop("checklist_text", "")
        _write_json(cdir / "user.json", data)

        # Save SOUL.md
        soul_path = cdir / "SOUL.md"
        if soul_text:
            soul_path.write_text(f"# Soul\n\n{soul_text}\n", encoding="utf-8")
        elif not soul_path.exists():
            # Seed a meaningful default for brand-new users
            soul_path.write_text(
                f"# Soul\n\n{load_prompt('default_soul')}\n",
                encoding="utf-8",
            )

        # Save USER.md
        user_path = cdir / "USER.md"
        if user_text:
            user_path.write_text(f"# User\n\n{user_text}\n", encoding="utf-8")
        elif not user_path.exists():
            user_path.write_text(
                f"# User\n\n{load_prompt('default_user')}\n",
                encoding="utf-8",
            )

        # Save HEARTBEAT.md
        checklist_path = cdir / "HEARTBEAT.md"
        if checklist_text:
            checklist_path.write_text(f"# Checklist\n\n{checklist_text}\n", encoding="utf-8")
        elif not checklist_path.exists():
            checklist_path.write_text(
                f"# Checklist\n\n{load_prompt('default_checklist')}\n",
                encoding="utf-8",
            )

        # Ensure subdirectories exist
        (cdir / "memory").mkdir(exist_ok=True)
        (cdir / "sessions").mkdir(exist_ok=True)
        (cdir / "estimates").mkdir(exist_ok=True)
        (cdir / "heartbeat").mkdir(exist_ok=True)

        # Initialize MEMORY.md if it doesn't exist
        mem_path = cdir / "memory" / "MEMORY.md"
        if not mem_path.exists():
            mem_path.write_text("# Long-term Memory\n", encoding="utf-8")

    def _update_index(self, user: UserData) -> None:
        """Update user_index.json with channel mapping."""
        idx_path = _index_path()
        index: dict[str, int] = _read_json(idx_path, {})
        if user.channel_identifier:
            key = f"{user.preferred_channel}:{user.channel_identifier}"
            index[key] = user.id
        _write_json(idx_path, index)

    def link_channel(self, channel: str, channel_identifier: str, user_id: int) -> None:
        """Add a channel mapping to the index for an existing user."""
        idx_path = _index_path()
        index: dict[str, int] = _read_json(idx_path, {})
        key = f"{channel}:{channel_identifier}"
        index[key] = user_id
        _write_json(idx_path, index)

    def _next_user_id(self) -> int:
        """Get the next user ID by scanning existing directories."""
        dirs = self._all_dirs()
        if not dirs:
            return 1
        return max(int(d.name) for d in dirs) + 1

    async def get_by_id(self, user_id: int) -> UserData | None:
        """Get a user by ID."""
        return self._load(user_id)

    async def get_by_user_id(self, user_id: str) -> UserData | None:
        """Get a user by user_id (scans all users)."""
        for cdir in self._all_dirs():
            cid = int(cdir.name)
            c = self._load(cid)
            if c and c.user_id == user_id:
                return c
        return None

    async def get_by_channel(self, channel_identifier: str) -> UserData | None:
        """Get a user by channel_identifier using the index."""
        idx_path = _index_path()
        index: dict[str, int] = _read_json(idx_path, {})
        # Try all channel prefixes
        for key, cid in index.items():
            if key.endswith(f":{channel_identifier}"):
                return self._load(cid)
        # Fallback: scan directories
        for cdir in self._all_dirs():
            cid = int(cdir.name)
            c = self._load(cid)
            if c and c.channel_identifier == channel_identifier:
                self._update_index(c)
                return c
        return None

    async def create(
        self,
        user_id: str,
        channel_identifier: str = "",
        preferred_channel: str = "telegram",
        **kwargs: Any,
    ) -> UserData:
        """Create a new user."""
        async with self._lock:
            cid = self._next_user_id()
            now = datetime.datetime.now(datetime.UTC)
            user = UserData(
                id=cid,
                user_id=user_id,
                channel_identifier=channel_identifier,
                preferred_channel=preferred_channel,
                created_at=now,
                updated_at=now,
                **kwargs,
            )
            self._save(user)
            self._update_index(user)
            return user

    async def update(self, user_id: int, **fields: Any) -> UserData | None:
        """Update user fields."""
        async with self._lock:
            user = self._load(user_id)
            if user is None:
                return None
            for key, value in fields.items():
                if hasattr(user, key) and value is not None:
                    setattr(user, key, value)
            user.updated_at = datetime.datetime.now(datetime.UTC)
            self._save(user)
            if "channel_identifier" in fields or "preferred_channel" in fields:
                self._update_index(user)
            return user

    async def list_all(self) -> list[UserData]:
        """List all users."""
        result: list[UserData] = []
        for cdir in self._all_dirs():
            cid = int(cdir.name)
            c = self._load(cid)
            if c:
                result.append(c)
        return result


# ---------------------------------------------------------------------------
# FileMemoryStore
# ---------------------------------------------------------------------------

# Regex to parse MEMORY.md entries: "- key: value (confidence: X.X)"
# Uses greedy match for value (.+) to handle values containing parentheses,
# then anchors on the final "(confidence: ...)" suffix.
_MEMORY_LINE_RE = re.compile(r"^-\s+(.+?):\s+(.+)\s+\(confidence:\s+([\d.]+)\)\s*$")
_CATEGORY_RE = re.compile(r"^##\s+(.+)$")


class FileMemoryStore:
    """File-based memory storage using MEMORY.md. Replaces Memory model."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _memory_path(self) -> Path:
        return _user_dir(self.user_id) / "memory" / "MEMORY.md"

    @property
    def _history_path(self) -> Path:
        return _user_dir(self.user_id) / "memory" / "HISTORY.md"

    @property
    def _soul_path(self) -> Path:
        return _user_dir(self.user_id) / "SOUL.md"

    def _parse_memory_md(self) -> list[MemoryFact]:
        """Parse MEMORY.md into a list of MemoryFact objects."""
        if not self._memory_path.exists():
            return []
        content = self._memory_path.read_text(encoding="utf-8")
        facts: list[MemoryFact] = []
        current_category = "general"
        for line in content.splitlines():
            cat_match = _CATEGORY_RE.match(line)
            if cat_match:
                heading = cat_match.group(1).strip()
                # Normalize known headings
                if heading == "Long-term Memory":
                    continue
                current_category = heading.lower()
                continue
            fact_match = _MEMORY_LINE_RE.match(line)
            if fact_match:
                facts.append(
                    MemoryFact(
                        key=fact_match.group(1).strip(),
                        value=fact_match.group(2).strip(),
                        confidence=float(fact_match.group(3)),
                        category=current_category,
                    )
                )
        return facts

    def _write_memory_md(self, facts: list[MemoryFact]) -> None:
        """Write facts back to MEMORY.md, grouped by category."""
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        categories: dict[str, list[MemoryFact]] = {}
        for fact in facts:
            categories.setdefault(fact.category, []).append(fact)

        lines: list[str] = ["# Long-term Memory", ""]
        for cat in sorted(categories.keys()):
            lines.append(f"## {cat.title()}")
            for f in categories[cat]:
                lines.append(f"- {f.key}: {f.value} (confidence: {f.confidence})")
            lines.append("")
        self._memory_path.write_text("\n".join(lines), encoding="utf-8")

    async def save_memory(
        self,
        key: str,
        value: str,
        category: str = "general",
        confidence: float = 1.0,
        source_message_id: int | None = None,
    ) -> MemoryFact:
        """Save or update a memory fact."""
        async with self._lock:
            facts = self._parse_memory_md()
            # Upsert: update if key exists, else append
            for fact in facts:
                if fact.key == key:
                    fact.value = value
                    fact.category = category
                    fact.confidence = confidence
                    self._write_memory_md(facts)
                    return fact
            new_fact = MemoryFact(key=key, value=value, category=category, confidence=confidence)
            facts.append(new_fact)
            self._write_memory_md(facts)
            return new_fact

    async def recall_memories(
        self,
        query: str,
        category: str | None = None,
        limit: int = 20,
    ) -> list[MemoryFact]:
        """Case-insensitive keyword search over keys and values."""
        facts = self._parse_memory_md()
        if category:
            facts = [f for f in facts if f.category == category]
        pattern = query.lower()
        matched = [f for f in facts if pattern in f.key.lower() or pattern in f.value.lower()]
        matched.sort(key=lambda f: f.confidence, reverse=True)
        return matched[:limit]

    async def get_all_memories(
        self,
        category: str | None = None,
    ) -> list[MemoryFact]:
        """Get all memory facts, optionally filtered by category."""
        facts = self._parse_memory_md()
        if category:
            facts = [f for f in facts if f.category == category]
        return facts

    async def delete_memory(self, key: str) -> bool:
        """Delete a specific memory. Returns True if found and deleted."""
        async with self._lock:
            facts = self._parse_memory_md()
            original_len = len(facts)
            facts = [f for f in facts if f.key != key]
            if len(facts) == original_len:
                return False
            self._write_memory_md(facts)
            return True

    async def build_memory_context(self, query: str | None = None) -> str:
        """Build a MEMORY.md-style text block for injection into the agent prompt."""
        if query:
            memories = await self.recall_memories(query)
        else:
            memories = await self.get_all_memories()

        client_store = ClientStore(self.user_id)
        clients = await client_store.list_all()

        lines: list[str] = []
        if memories:
            lines.append("## Known Facts")
            for m in memories:
                lines.append(f"- {m.key}: {m.value} (confidence: {m.confidence})")
            lines.append("")
        if clients:
            lines.append("## Clients")
            for c in clients:
                parts = [c.name]
                if c.phone:
                    parts.append(f"({c.phone})")
                if c.address:
                    parts.append(f": {c.address}")
                if c.notes:
                    parts.append(f", {c.notes}")
                lines.append(f"- {' '.join(parts)}")
            lines.append("")
        return "\n".join(lines)

    async def append_history(self, entry: str) -> None:
        """Append an entry to HISTORY.md."""
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def read_soul(self) -> str:
        """Read SOUL.md content."""
        if not self._soul_path.exists():
            return ""
        raw = self._soul_path.read_text(encoding="utf-8").strip()
        if raw.startswith("# Soul"):
            raw = raw[len("# Soul") :].strip()
        return raw

    def write_soul(self, content: str) -> None:
        """Write SOUL.md content."""
        self._soul_path.parent.mkdir(parents=True, exist_ok=True)
        self._soul_path.write_text(f"# Soul\n\n{content}\n", encoding="utf-8")

    @property
    def _user_path(self) -> Path:
        return _user_dir(self.user_id) / "USER.md"

    def read_user(self) -> str:
        """Read USER.md content."""
        if not self._user_path.exists():
            return ""
        raw = self._user_path.read_text(encoding="utf-8").strip()
        if raw.startswith("# User"):
            raw = raw[len("# User") :].strip()
        return raw

    def write_user(self, content: str) -> None:
        """Write USER.md content."""
        self._user_path.parent.mkdir(parents=True, exist_ok=True)
        self._user_path.write_text(f"# User\n\n{content}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# FileSessionStore
# ---------------------------------------------------------------------------


class FileSessionStore:
    """File-based session storage using JSONL files. Replaces Conversation + Message models."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _sessions_dir(self) -> Path:
        return _user_dir(self.user_id) / "sessions"

    def _session_path(self, session_id: str) -> Path:
        return self._sessions_dir / f"{session_id}.jsonl"

    def _list_session_files(self) -> list[Path]:
        """List all session files, sorted by name."""
        sdir = self._sessions_dir
        if not sdir.exists():
            return []
        return sorted(sdir.glob("*.jsonl"))

    def _load_session(self, session_id: str) -> SessionState | None:
        """Load a session from disk."""
        path = self._session_path(session_id)
        if not path.exists():
            return None
        lines = _read_jsonl(path)
        if not lines:
            return None

        metadata = lines[0] if lines and lines[0].get("_type") == "metadata" else {}
        messages: list[StoredMessage] = []
        for line in lines:
            if line.get("_type") == "metadata":
                continue
            messages.append(StoredMessage.model_validate(line))

        return SessionState(
            session_id=session_id,
            user_id=self.user_id,
            messages=messages,
            is_active=metadata.get("is_active", True),
            created_at=metadata.get("created_at", ""),
            last_message_at=metadata.get("last_message_at", ""),
            last_compacted_seq=metadata.get("last_compacted_seq", 0),
            channel=metadata.get("channel", ""),
        )

    def _write_metadata(self, session_id: str, meta: dict[str, Any]) -> None:
        """Rewrite the metadata line of a session file."""
        path = self._session_path(session_id)
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return
        # Replace the first line if it's metadata
        first = json.loads(lines[0]) if lines else {}
        if first.get("_type") == "metadata":
            first.update(meta)
            lines[0] = json.dumps(first, default=str)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def get_or_create_session(
        self,
        timeout_hours: int | None = None,
    ) -> tuple[SessionState, bool]:
        """Get active session or create new one. Returns (session, is_new)."""
        timeout = timeout_hours or settings.conversation_timeout_hours
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=timeout)

        # Find the most recent active session
        for path in reversed(self._list_session_files()):
            session_id = path.stem
            session = self._load_session(session_id)
            if session is None or not session.is_active:
                continue
            if session.last_message_at:
                try:
                    last_at = datetime.datetime.fromisoformat(session.last_message_at)
                    if last_at.tzinfo is None:
                        last_at = last_at.replace(tzinfo=datetime.UTC)
                    if last_at >= cutoff:
                        # Update last_message_at
                        now = datetime.datetime.now(datetime.UTC).isoformat()
                        self._write_metadata(session_id, {"last_message_at": now})
                        session.last_message_at = now
                        return session, False
                except (ValueError, TypeError):
                    pass

        # Create new session
        now = datetime.datetime.now(datetime.UTC)
        ts = int(now.timestamp())
        session_id = f"{self.user_id}_{ts}"
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        meta = {
            "_type": "metadata",
            "session_id": session_id,
            "user_id": self.user_id,
            "created_at": now.isoformat(),
            "last_message_at": now.isoformat(),
            "is_active": True,
            "last_compacted_seq": 0,
        }
        path.write_text(json.dumps(meta, default=str) + "\n", encoding="utf-8")

        session = SessionState(
            session_id=session_id,
            user_id=self.user_id,
            is_active=True,
            created_at=now.isoformat(),
            last_message_at=now.isoformat(),
            last_compacted_seq=0,
        )
        return session, True

    async def add_message(
        self,
        session: SessionState,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Append a message to the session JSONL file."""
        async with self._lock:
            seq = len(session.messages) + 1
            now = datetime.datetime.now(datetime.UTC).isoformat()
            msg = StoredMessage(
                direction=direction,
                body=body,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                timestamp=now,
                seq=seq,
            )
            _append_jsonl(self._session_path(session.session_id), msg.model_dump())
            session.messages.append(msg)
            # Update last_message_at and channel (if provided)
            meta_update: dict[str, str] = {"last_message_at": now}
            if channel:
                meta_update["channel"] = channel
                session.channel = channel
            self._write_metadata(session.session_id, meta_update)
            session.last_message_at = now
            return msg

    async def update_message(
        self,
        session: SessionState,
        seq: int,
        **updates: Any,
    ) -> None:
        """Update a message by seq number (full file rewrite)."""
        async with self._lock:
            path = self._session_path(session.session_id)
            lines = _read_jsonl(path)
            for i, line in enumerate(lines):
                if line.get("_type") == "metadata":
                    continue
                if line.get("seq") == seq:
                    line.update(updates)
                    lines[i] = line
                    break
            # Rewrite file
            path.write_text(
                "\n".join(json.dumps(line, default=str) for line in lines) + "\n",
                encoding="utf-8",
            )
            # Update in-memory
            for msg in session.messages:
                if msg.seq == seq:
                    for k, v in updates.items():
                        if hasattr(msg, k):
                            setattr(msg, k, v)
                    break

    def get_last_inbound_timestamp(self) -> datetime.datetime | None:
        """Scan sessions for the most recent inbound message timestamp."""
        latest: datetime.datetime | None = None
        for path in self._list_session_files():
            for line in _read_jsonl(path):
                if line.get("_type") == "metadata":
                    continue
                if line.get("direction") != "inbound":
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(line["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=datetime.UTC)
                    if latest is None or ts > latest:
                        latest = ts
                except (ValueError, KeyError, TypeError):
                    pass
        return latest

    def get_last_outbound_timestamp(self) -> datetime.datetime | None:
        """Scan sessions for the most recent outbound message timestamp."""
        latest: datetime.datetime | None = None
        for path in self._list_session_files():
            for line in _read_jsonl(path):
                if line.get("_type") == "metadata":
                    continue
                if line.get("direction") != "outbound":
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(line["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=datetime.UTC)
                    if latest is None or ts > latest:
                        latest = ts
                except (ValueError, KeyError, TypeError):
                    pass
        return latest

    async def update_compaction_seq(self, session: SessionState, seq: int) -> None:
        """Update the last_compacted_seq in session metadata."""
        self._write_metadata(session.session_id, {"last_compacted_seq": seq})
        session.last_compacted_seq = seq

    def get_recent_messages(self, count: int = 5) -> list[StoredMessage]:
        """Get the most recent messages across all sessions."""
        all_msgs: list[StoredMessage] = []
        for path in reversed(self._list_session_files()):
            lines = _read_jsonl(path)
            for line in lines:
                if line.get("_type") == "metadata":
                    continue
                all_msgs.append(StoredMessage.model_validate(line))
            if len(all_msgs) >= count:
                break
        # Sort by timestamp descending, take the most recent
        all_msgs.sort(key=lambda m: m.timestamp, reverse=True)
        return list(reversed(all_msgs[:count]))

    def get_other_session_messages(
        self,
        exclude_session_id: str,
        count: int = 5,
    ) -> list[StoredMessage]:
        """Get recent messages from sessions other than *exclude_session_id*."""
        all_msgs: list[StoredMessage] = []
        for path in reversed(self._list_session_files()):
            if path.stem == exclude_session_id:
                continue
            lines = _read_jsonl(path)
            for line in lines:
                if line.get("_type") == "metadata":
                    continue
                all_msgs.append(StoredMessage.model_validate(line))
            if len(all_msgs) >= count:
                break
        all_msgs.sort(key=lambda m: m.timestamp, reverse=True)
        return list(reversed(all_msgs[:count]))


# ---------------------------------------------------------------------------
# ClientStore
# ---------------------------------------------------------------------------


class ClientStore:
    """File-based client storage. Replaces Client model."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _path(self) -> Path:
        return _user_dir(self.user_id) / "clients.json"

    def _load_all(self) -> list[dict[str, Any]]:
        return _read_json(self._path, [])

    async def list_all(self) -> list[ClientData]:
        """List all clients."""
        items = self._load_all()
        return [ClientData.model_validate(item) for item in items]

    async def get(self, client_id: str) -> ClientData | None:
        """Get a client by ID (slug)."""
        for item in self._load_all():
            if item.get("id") == client_id:
                return ClientData.model_validate(item)
        return None

    async def create(
        self,
        name: str = "",
        phone: str = "",
        email: str = "",
        address: str = "",
        notes: str = "",
        folder_scheme: str = "by_client",
    ) -> ClientData:
        """Create a new client with a slug-based ID."""
        async with self._lock:
            items = self._load_all()
            base_slug = make_client_slug(name, address, folder_scheme)
            if not base_slug:
                base_slug = "client"
            existing_ids = {item.get("id", "") for item in items}
            cid = _unique_slug(base_slug, existing_ids)
            client = ClientData(
                id=cid,
                name=name,
                phone=phone,
                email=email,
                address=address,
                notes=notes,
            )
            items.append(client.model_dump())
            _write_json(self._path, items)
            return client

    async def update(self, client_id: str, **fields: Any) -> ClientData | None:
        """Update a client's fields."""
        async with self._lock:
            items = self._load_all()
            for i, item in enumerate(items):
                if item.get("id") == client_id:
                    for k, v in fields.items():
                        if v is not None:
                            item[k] = v
                    items[i] = item
                    _write_json(self._path, items)
                    return ClientData.model_validate(item)
            return None

    async def delete(self, client_id: str) -> bool:
        """Delete a client. Returns True if found and deleted."""
        async with self._lock:
            items = self._load_all()
            original_len = len(items)
            items = [i for i in items if i.get("id") != client_id]
            if len(items) == original_len:
                return False
            _write_json(self._path, items)
            return True


# ---------------------------------------------------------------------------
# EstimateStore
# ---------------------------------------------------------------------------


class EstimateStore:
    """File-based estimate storage. Replaces Estimate + EstimateLineItem models.

    Estimates are organized under client subdirectories::

        estimates/
          {client_slug}/
            EST-0001.json
          unsorted/
            EST-0003.json
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _estimates_dir(self) -> Path:
        return _user_dir(self.user_id) / "estimates"

    def _estimate_path(self, estimate_id: str, client_id: str | None = None) -> Path:
        folder = client_id if client_id else "unsorted"
        return self._estimates_dir / folder / f"{estimate_id}.json"

    def _find_estimate_path(self, estimate_id: str) -> Path | None:
        """Search all subdirectories for an estimate by ID."""
        edir = self._estimates_dir
        if not edir.exists():
            return None
        for path in edir.rglob(f"{estimate_id}.json"):
            return path
        return None

    async def list_all(self) -> list[EstimateData]:
        """List all estimates across all client subdirectories."""
        edir = self._estimates_dir
        if not edir.exists():
            return []
        result: list[EstimateData] = []
        for path in sorted(edir.rglob("*.json")):
            data = _read_json(path)
            if data:
                result.append(EstimateData.model_validate(data))
        return result

    async def get(self, estimate_id: str) -> EstimateData | None:
        """Get an estimate by ID."""
        path = self._find_estimate_path(estimate_id)
        if path is None:
            return None
        data = _read_json(path)
        if data is None:
            return None
        return EstimateData.model_validate(data)

    def _next_estimate_number(self, existing: list[EstimateData]) -> int:
        """Get the next sequential estimate number across all estimates."""
        max_num = 0
        for e in existing:
            # Parse number from "EST-0001" format
            if e.id.startswith("EST-"):
                try:
                    num = int(e.id[4:])
                    max_num = max(max_num, num)
                except ValueError:
                    pass
        return max_num + 1

    async def create(
        self,
        description: str = "",
        total_amount: float = 0.0,
        status: str = EstimateStatus.DRAFT,
        client_id: str | None = None,
        line_items: list[dict[str, Any]] | None = None,
    ) -> EstimateData:
        """Create a new estimate.

        Args:
            client_id: Client slug used as the subdirectory name.
                       Falls back to "unsorted" when not provided.
        """
        async with self._lock:
            existing = await self.list_all()
            num = self._next_estimate_number(existing)
            eid = f"EST-{num:04d}"

            items: list[EstimateLineItemData] = []
            if line_items:
                for i, li in enumerate(line_items, 1):
                    items.append(
                        EstimateLineItemData(
                            id=i,
                            description=str(li.get("description", "")),
                            quantity=float(li.get("quantity", 1)),
                            unit_price=float(li.get("unit_price", 0)),
                            total=float(li.get("total", 0)),
                        )
                    )

            estimate = EstimateData(
                id=eid,
                user_id=self.user_id,
                client_id=client_id,
                description=description,
                total_amount=total_amount,
                status=status,
                line_items=items,
            )
            path = self._estimate_path(eid, client_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(path, estimate.model_dump())
            return estimate

    async def update(self, estimate_id: str, **fields: Any) -> EstimateData | None:
        """Update an estimate's fields."""
        async with self._lock:
            estimate = await self.get(estimate_id)
            if estimate is None:
                return None
            data = estimate.model_dump()
            data.update({k: v for k, v in fields.items() if v is not None})
            path = self._find_estimate_path(estimate_id)
            if path is None:
                return None
            _write_json(path, data)
            return EstimateData.model_validate(data)


# ---------------------------------------------------------------------------
# MediaStore
# ---------------------------------------------------------------------------


class MediaStore:
    """File-based media file manifest. Replaces MediaFile model."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _path(self) -> Path:
        return _user_dir(self.user_id) / "media.json"

    def _load_all(self) -> list[dict[str, Any]]:
        return _read_json(self._path, [])

    async def list_all(self) -> list[MediaData]:
        """List all media files."""
        return [MediaData.model_validate(item) for item in self._load_all()]

    def _next_media_id(self, items: list[dict[str, Any]]) -> str:
        """Generate the next sequential media ID string."""
        max_num = 0
        for item in items:
            mid = str(item.get("id", ""))
            if mid.startswith("media-"):
                try:
                    num = int(mid[6:])
                    max_num = max(max_num, num)
                except ValueError:
                    pass
        return f"media-{max_num + 1:03d}"

    async def create(
        self,
        original_url: str = "",
        mime_type: str = "",
        processed_text: str = "",
        storage_url: str = "",
        storage_path: str = "",
        message_id: int | None = None,
    ) -> MediaData:
        """Create a media file record."""
        async with self._lock:
            items = self._load_all()
            mid = self._next_media_id(items)
            media = MediaData(
                id=mid,
                user_id=self.user_id,
                message_id=message_id,
                original_url=original_url,
                mime_type=mime_type,
                processed_text=processed_text,
                storage_url=storage_url,
                storage_path=storage_path,
            )
            items.append(media.model_dump())
            _write_json(self._path, items)
            return media

    async def get_by_url(self, original_url: str) -> MediaData | None:
        """Find a media record by original URL."""
        for item in self._load_all():
            if item.get("original_url") == original_url:
                return MediaData.model_validate(item)
        return None

    async def update(self, media_id: str, **fields: Any) -> MediaData | None:
        """Update a media file record."""
        async with self._lock:
            items = self._load_all()
            for i, item in enumerate(items):
                if str(item.get("id", "")) == media_id:
                    for k, v in fields.items():
                        if v is not None:
                            item[k] = v
                    items[i] = item
                    _write_json(self._path, items)
                    return MediaData.model_validate(item)
            return None

    async def count_by_path_prefix(self, prefix: str) -> int:
        """Count media files whose storage_path starts with prefix."""
        return sum(
            1 for item in self._load_all() if item.get("storage_path", "").startswith(prefix)
        )


# ---------------------------------------------------------------------------
# HeartbeatStore
# ---------------------------------------------------------------------------


class HeartbeatStore:
    """File-based heartbeat storage.

    Checklist items are stored in ``HEARTBEAT.md`` (the user's markdown
    checklist file), making it the single source of truth for both the
    heartbeat engine and the UI editor.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _checklist_md_path(self) -> Path:
        return _user_dir(self.user_id) / "HEARTBEAT.md"

    @property
    def _log_path(self) -> Path:
        return _user_dir(self.user_id) / "heartbeat" / "log.jsonl"

    # -- HEARTBEAT.md I/O -------------------------------------------------

    def read_checklist_md(self) -> str:
        """Read raw HEARTBEAT.md content. Returns empty string if missing."""
        if self._checklist_md_path.exists():
            try:
                return self._checklist_md_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Failed to read HEARTBEAT.md for user %d", self.user_id)
        return ""

    def _write_checklist_md(self, content: str) -> None:
        """Write content to HEARTBEAT.md, creating parent dirs as needed."""
        self._checklist_md_path.parent.mkdir(parents=True, exist_ok=True)
        self._checklist_md_path.write_text(content, encoding="utf-8")

    # -- Structured checklist access (reads from HEARTBEAT.md) ------------

    def _parse_checklist_md(self) -> list[dict[str, Any]]:
        """Parse HEARTBEAT.md into a list of item dicts with ids.

        Recognises lines matching ``- [ ] text`` or ``- [x] text`` as
        checklist items.  An optional ``(schedule)`` suffix is extracted.
        Each item gets an id derived from its 1-based position among
        checklist items.  IDs are stable within a single read but may
        shift after mutations (add/delete).
        """
        content = self.read_checklist_md()
        if not content:
            return []
        items: list[dict[str, Any]] = []
        item_id = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ] ") or stripped.startswith("- [x] "):
                item_id += 1
                is_checked = stripped.startswith("- [x] ")
                text = stripped[6:].strip()
                schedule = ChecklistSchedule.DAILY
                if text.endswith(")"):
                    paren_idx = text.rfind("(")
                    if paren_idx > 0:
                        maybe_sched = text[paren_idx + 1 : -1].strip().lower()
                        if maybe_sched in list(ChecklistSchedule):
                            schedule = maybe_sched
                            text = text[:paren_idx].strip()
                status = ChecklistStatus.COMPLETED if is_checked else ChecklistStatus.ACTIVE
                items.append(
                    {
                        "id": item_id,
                        "user_id": self.user_id,
                        "description": text,
                        "schedule": schedule,
                        "status": status,
                        "last_triggered_at": None,
                        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    }
                )
        return items

    async def get_checklist(self) -> list[ChecklistItem]:
        """Get all checklist items parsed from HEARTBEAT.md."""
        return [ChecklistItem.model_validate(item) for item in self._parse_checklist_md()]

    async def add_checklist_item(
        self,
        description: str,
        schedule: str = ChecklistSchedule.DAILY,
    ) -> ChecklistItem:
        """Add a checklist item by appending a line to HEARTBEAT.md."""
        async with self._lock:
            content = self.read_checklist_md()
            if not content:
                content = "# Checklist\n\n"
            if not content.endswith("\n"):
                content += "\n"
            schedule_note = f" ({schedule})" if schedule != ChecklistSchedule.DAILY else ""
            content += f"- [ ] {description}{schedule_note}\n"
            self._write_checklist_md(content)

            items = self._parse_checklist_md()
            return ChecklistItem.model_validate(items[-1])

    async def update_checklist_item(
        self,
        item_id: int,
        **fields: Any,
    ) -> ChecklistItem | None:
        """Update a checklist item in HEARTBEAT.md by id.

        Supports updating description, schedule, and status.  When status
        changes to completed the checkbox is checked (``[x]``).
        """
        async with self._lock:
            items = self._parse_checklist_md()
            target = None
            for item in items:
                if item["id"] == item_id:
                    target = item
                    break
            if target is None:
                return None

            for k, v in fields.items():
                if v is not None:
                    target[k] = v

            self._rebuild_checklist_md(items)
            return ChecklistItem.model_validate(target)

    async def delete_checklist_item(self, item_id: int) -> bool:
        """Delete a checklist item from HEARTBEAT.md by id."""
        async with self._lock:
            items = self._parse_checklist_md()
            original_len = len(items)
            items = [i for i in items if i["id"] != item_id]
            if len(items) == original_len:
                return False
            self._rebuild_checklist_md(items)
            return True

    def _rebuild_checklist_md(self, items: list[dict[str, Any]]) -> None:
        """Rebuild HEARTBEAT.md from a list of item dicts.

        Preserves non-checklist-item lines (headings, blank lines, prose)
        from the original file and replaces only the checklist item lines.
        """
        content = self.read_checklist_md()
        new_lines: list[str] = []
        item_idx = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ] ") or stripped.startswith("- [x] "):
                if item_idx < len(items):
                    item = items[item_idx]
                    checkbox = "[x]" if item.get("status") == ChecklistStatus.COMPLETED else "[ ]"
                    desc = item["description"]
                    sched = item.get("schedule", ChecklistSchedule.DAILY)
                    suffix = f" ({sched})" if sched != ChecklistSchedule.DAILY else ""
                    new_lines.append(f"- {checkbox} {desc}{suffix}")
                    item_idx += 1
            else:
                new_lines.append(line)
        while item_idx < len(items):
            item = items[item_idx]
            checkbox = "[x]" if item.get("status") == ChecklistStatus.COMPLETED else "[ ]"
            desc = item["description"]
            sched = item.get("schedule", ChecklistSchedule.DAILY)
            suffix = f" ({sched})" if sched != ChecklistSchedule.DAILY else ""
            new_lines.append(f"- {checkbox} {desc}{suffix}")
            item_idx += 1
        result = "\n".join(new_lines)
        if not result.endswith("\n"):
            result += "\n"
        self._write_checklist_md(result)

    async def log_heartbeat(self) -> None:
        """Append to heartbeat log."""
        entry = HeartbeatLogEntry(user_id=self.user_id)
        _append_jsonl(self._log_path, entry.model_dump())

    async def get_daily_count(self) -> int:
        """Count heartbeat messages sent today (UTC)."""
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        count = 0
        for line in _read_jsonl(self._log_path):
            created = line.get("created_at", "")
            if created.startswith(today):
                count += 1
        return count

    async def get_recent_logs(
        self,
        since: datetime.datetime,
    ) -> list[HeartbeatLogEntry]:
        """Get heartbeat logs since a given time."""
        result: list[HeartbeatLogEntry] = []
        for line in _read_jsonl(self._log_path):
            try:
                ts = datetime.datetime.fromisoformat(line.get("created_at", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.UTC)
                if ts >= since:
                    result.append(HeartbeatLogEntry.model_validate(line))
            except (ValueError, TypeError):
                pass
        return result


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------

_SEEN_MAX = 10_000


class IdempotencyStore:
    """Global idempotency tracking for webhook deduplication."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def _path(self) -> Path:
        return _data_dir() / "seen_messages.json"

    def _load(self) -> list[str]:
        return _read_json(self._path, [])

    def has_seen(self, external_id: str) -> bool:
        """Check if an external message ID has been seen."""
        return external_id in set(self._load())

    async def mark_seen(self, external_id: str) -> None:
        """Mark an external message ID as seen. Caps at 10K entries."""
        async with self._lock:
            seen = self._load()
            if external_id in seen:
                return
            seen.append(external_id)
            if len(seen) > _SEEN_MAX:
                seen = seen[-_SEEN_MAX:]
            _write_json(self._path, seen)


# ---------------------------------------------------------------------------
# LLMUsageStore
# ---------------------------------------------------------------------------


class LLMUsageStore:
    """Append-only LLM usage log. Replaces LLMUsageLog model."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    @property
    def _path(self) -> Path:
        return _user_dir(self.user_id) / "llm_usage.jsonl"

    def log(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        purpose: str,
    ) -> None:
        """Append a usage log entry."""
        entry = {
            "user_id": self.user_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "purpose": purpose,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        _append_jsonl(self._path, entry)


# ---------------------------------------------------------------------------
# ToolConfigStore
# ---------------------------------------------------------------------------


class ToolConfigStore:
    """File-based storage for per-user tool configuration.

    Stores a list of ``ToolConfigEntry`` objects in
    ``data/users/{id}/tool_config.json``.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._lock = asyncio.Lock()

    @property
    def _path(self) -> Path:
        return _user_dir(self.user_id) / "tool_config.json"

    async def load(self) -> list[ToolConfigEntry]:
        """Load tool config entries. Returns empty list if no config exists."""
        raw = _read_json(self._path, [])
        return [ToolConfigEntry.model_validate(item) for item in raw]

    async def save(self, entries: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Save tool config entries and return them."""
        async with self._lock:
            _write_json(self._path, [e.model_dump() for e in entries])
        return entries

    async def get_disabled_tool_names(self) -> set[str]:
        """Return the set of tool group names that are disabled."""
        entries = await self.load()
        return {e.name for e in entries if not e.enabled}


# ---------------------------------------------------------------------------
# Module-level singletons / factories
# ---------------------------------------------------------------------------

_user_store: UserStore | None = None
_memory_stores: dict[int, FileMemoryStore] = {}
_session_stores: dict[int, FileSessionStore] = {}
_idempotency_store: IdempotencyStore | None = None


def get_user_store() -> UserStore:
    """Get or create the global UserStore."""
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def get_memory_store(user_id: int) -> FileMemoryStore:
    """Get or create a FileMemoryStore for a user."""
    if user_id not in _memory_stores:
        _memory_stores[user_id] = FileMemoryStore(user_id)
    return _memory_stores[user_id]


def get_session_store(user_id: int) -> FileSessionStore:
    """Get or create a FileSessionStore for a user."""
    if user_id not in _session_stores:
        _session_stores[user_id] = FileSessionStore(user_id)
    return _session_stores[user_id]


def get_idempotency_store() -> IdempotencyStore:
    """Get or create the global IdempotencyStore."""
    global _idempotency_store
    if _idempotency_store is None:
        _idempotency_store = IdempotencyStore()
    return _idempotency_store


def reset_stores() -> None:
    """Reset all cached store instances. Used by tests."""
    global _user_store, _idempotency_store
    _user_store = None
    _memory_stores.clear()
    _session_stores.clear()
    _idempotency_store = None
