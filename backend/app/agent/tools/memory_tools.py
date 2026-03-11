from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent.memory import delete_memory, recall_memories, save_memory
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult, ToolTags
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


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


def create_memory_tools(user_id: int) -> list[Tool]:
    """Create memory-related tools for the agent."""

    async def save_fact(key: str, value: str, category: str = "general") -> ToolResult:
        """Save a fact to memory."""
        memory = await save_memory(user_id, key=key, value=value, category=category)
        return ToolResult(content=f"Saved: {memory.key} = {memory.value}")

    async def recall_facts(query: str, category: str | None = None) -> ToolResult:
        """Search memory for facts matching a query."""
        memories = await recall_memories(user_id, query=query, category=category)
        if not memories:
            return ToolResult(content="No matching facts found.")
        lines = [f"- {m.key}: {m.value}" for m in memories]
        return ToolResult(content="\n".join(lines))

    async def forget_fact(key: str) -> ToolResult:
        """Delete a fact from memory."""
        deleted = await delete_memory(user_id, key=key)
        if deleted:
            return ToolResult(content=f"Deleted: {key}")
        return ToolResult(
            content=f"Not found: {key}", is_error=True, error_kind=ToolErrorKind.NOT_FOUND
        )

    return [
        Tool(
            name=ToolName.SAVE_FACT,
            description=(
                "Save a key-value fact to the user's memory. "
                "Use for pricing, client info, preferences, etc."
            ),
            function=save_fact,
            params_model=SaveFactParams,
            tags={ToolTags.SAVES_MEMORY},
            usage_hint=("When you learn new information (rates, clients, preferences), save it."),
        ),
        Tool(
            name=ToolName.RECALL_FACTS,
            description="Search the user's memory for facts matching a query.",
            function=recall_facts,
            params_model=RecallFactsParams,
            usage_hint=(
                "When asked about the user's business, clients, or past work,"
                " search your memory first."
            ),
        ),
        Tool(
            name=ToolName.FORGET_FACT,
            description="Delete a fact from memory by key.",
            function=forget_fact,
            params_model=ForgetFactParams,
            usage_hint="When asked to forget or delete a specific fact, remove it.",
        ),
    ]


def _memory_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for memory tools, used by the registry."""
    return create_memory_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register("memory", _memory_factory)


_register()
