from sqlalchemy.orm import Session

from backend.app.agent.memory import delete_memory, recall_memories, save_memory
from backend.app.agent.tools.base import Tool


def create_memory_tools(db: Session, contractor_id: int) -> list[Tool]:
    """Create memory-related tools for the agent."""

    async def save_fact(key: str, value: str, category: str = "general") -> str:
        """Save a fact to memory."""
        memory = await save_memory(db, contractor_id, key=key, value=value, category=category)
        return f"Saved: {memory.key} = {memory.value}"

    async def recall_facts(query: str, category: str | None = None) -> str:
        """Search memory for facts matching a query."""
        memories = await recall_memories(db, contractor_id, query=query, category=category)
        if not memories:
            return "No matching facts found."
        lines = [f"- {m.key}: {m.value}" for m in memories]
        return "\n".join(lines)

    async def forget_fact(key: str) -> str:
        """Delete a fact from memory."""
        deleted = await delete_memory(db, contractor_id, key=key)
        if deleted:
            return f"Deleted: {key}"
        return f"Not found: {key}"

    return [
        Tool(
            name="save_fact",
            description="Save a key-value fact to the contractor's memory. Use for pricing, client info, preferences, etc.",
            function=save_fact,
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier for the fact"},
                    "value": {"type": "string", "description": "The fact value to remember"},
                    "category": {
                        "type": "string",
                        "enum": ["pricing", "client", "job", "general"],
                        "description": "Category for the fact",
                    },
                },
                "required": ["key", "value"],
            },
        ),
        Tool(
            name="recall_facts",
            description="Search the contractor's memory for facts matching a query.",
            function=recall_facts,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "category": {
                        "type": "string",
                        "enum": ["pricing", "client", "job", "general"],
                        "description": "Optional category filter",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="forget_fact",
            description="Delete a fact from memory by key.",
            function=forget_fact,
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key of the fact to delete"},
                },
                "required": ["key"],
            },
        ),
    ]
