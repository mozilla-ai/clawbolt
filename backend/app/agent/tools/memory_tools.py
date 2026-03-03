from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.agent.memory import delete_memory, recall_memories, save_memory
from backend.app.agent.tools.base import Tool, ToolResult, ToolTags
from backend.app.agent.tools.registry import ToolContext, ToolFactory, default_registry


class SaveFactParams(BaseModel):
    """Parameters for the save_fact tool."""

    key: str = Field(description="Short identifier for the fact")
    value: str = Field(description="The fact value to remember")
    category: Literal["pricing", "client", "job", "general"] = Field(
        default="general",
        description="Category for the fact",
    )


class RecallFactsParams(BaseModel):
    """Parameters for the recall_facts tool."""

    query: str = Field(description="Search query")
    category: Literal["pricing", "client", "job", "general"] | None = Field(
        default=None,
        description="Optional category filter",
    )


class ForgetFactParams(BaseModel):
    """Parameters for the forget_fact tool."""

    key: str = Field(description="Key of the fact to delete")


def create_memory_tools(db: Session, contractor_id: int) -> list[Tool]:
    """Create memory-related tools for the agent."""

    async def save_fact(key: str, value: str, category: str = "general") -> ToolResult:
        """Save a fact to memory."""
        memory = await save_memory(db, contractor_id, key=key, value=value, category=category)
        return ToolResult(content=f"Saved: {memory.key} = {memory.value}")

    async def recall_facts(query: str, category: str | None = None) -> ToolResult:
        """Search memory for facts matching a query."""
        memories = await recall_memories(db, contractor_id, query=query, category=category)
        if not memories:
            return ToolResult(content="No matching facts found.")
        lines = [f"- {m.key}: {m.value}" for m in memories]
        return ToolResult(content="\n".join(lines))

    async def forget_fact(key: str) -> ToolResult:
        """Delete a fact from memory."""
        deleted = await delete_memory(db, contractor_id, key=key)
        if deleted:
            return ToolResult(content=f"Deleted: {key}")
        return ToolResult(content=f"Not found: {key}", is_error=True)

    return [
        Tool(
            name="save_fact",
            description=(
                "Save a key-value fact to the contractor's memory. "
                "Use for pricing, client info, preferences, etc."
            ),
            function=save_fact,
            params_model=SaveFactParams,
            tags={ToolTags.SAVES_MEMORY},
        ),
        Tool(
            name="recall_facts",
            description="Search the contractor's memory for facts matching a query.",
            function=recall_facts,
            params_model=RecallFactsParams,
        ),
        Tool(
            name="forget_fact",
            description="Delete a fact from memory by key.",
            function=forget_fact,
            params_model=ForgetFactParams,
        ),
    ]


def _factory(ctx: ToolContext) -> list[Tool]:
    return create_memory_tools(ctx.db, ctx.contractor.id)


default_registry.register(ToolFactory(create=_factory))
