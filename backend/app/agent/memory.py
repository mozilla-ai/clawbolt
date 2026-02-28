from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.models import Client, Memory


async def save_memory(
    db: Session,
    contractor_id: int,
    key: str,
    value: str,
    category: str = "general",
    confidence: float = 1.0,
    source_message_id: int | None = None,
) -> Memory:
    """Save or update a memory fact. If key exists for this contractor, update it."""
    existing = (
        db.query(Memory).filter(Memory.contractor_id == contractor_id, Memory.key == key).first()
    )
    if existing:
        existing.value = value
        existing.category = category
        existing.confidence = confidence
        if source_message_id is not None:
            existing.source_message_id = source_message_id
        db.commit()
        db.refresh(existing)
        return existing

    memory = Memory(
        contractor_id=contractor_id,
        key=key,
        value=value,
        category=category,
        confidence=confidence,
        source_message_id=source_message_id,
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


async def recall_memories(
    db: Session,
    contractor_id: int,
    query: str,
    category: str | None = None,
    limit: int = 20,
) -> list[Memory]:
    """Recall memories relevant to a query using keyword matching (ILIKE)."""
    q = db.query(Memory).filter(Memory.contractor_id == contractor_id)
    if category:
        q = q.filter(Memory.category == category)

    pattern = f"%{query}%"
    q = q.filter(or_(Memory.key.ilike(pattern), Memory.value.ilike(pattern)))
    return list(q.order_by(Memory.confidence.desc()).limit(limit).all())


async def get_all_memories(
    db: Session,
    contractor_id: int,
    category: str | None = None,
) -> list[Memory]:
    """Get all memories for a contractor, optionally filtered by category."""
    q = db.query(Memory).filter(Memory.contractor_id == contractor_id)
    if category:
        q = q.filter(Memory.category == category)
    return list(q.order_by(Memory.updated_at.desc()).all())


async def delete_memory(db: Session, contractor_id: int, key: str) -> bool:
    """Delete a specific memory. Returns True if found and deleted."""
    memory = (
        db.query(Memory).filter(Memory.contractor_id == contractor_id, Memory.key == key).first()
    )
    if memory is None:
        return False
    db.delete(memory)
    db.commit()
    return True


async def build_memory_context(
    db: Session,
    contractor_id: int,
    query: str | None = None,
) -> str:
    """Build a MEMORY.md-style text block for injection into the agent prompt."""
    if query:
        memories = await recall_memories(db, contractor_id, query)
    else:
        memories = await get_all_memories(db, contractor_id)

    clients = db.query(Client).filter(Client.contractor_id == contractor_id).all()

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
