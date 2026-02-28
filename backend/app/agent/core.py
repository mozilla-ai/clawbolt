import json
import logging
from dataclasses import dataclass, field

from any_llm import acompletion
from sqlalchemy.orm import Session

from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt
from backend.app.agent.tools.base import Tool, tool_to_openai_schema
from backend.app.config import settings
from backend.app.models import Contractor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are Backshop, an AI assistant for solo contractors.

## About {contractor_name}
{soul_prompt}

## Your Memory
{memory_context}

## Instructions
- Be concise and practical. Contractors are busy.
- When you learn new information (rates, clients, preferences), save it using the save_fact tool.
- When asked to recall something, use recall_facts to search your memory.
- When asked for an estimate, gather the details and use the appropriate tools.
- Always be helpful, friendly, and professional.
- Keep SMS replies under 160 characters when possible (single SMS segment).
"""


@dataclass
class AgentResponse:
    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    memories_saved: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[dict[str, object]] = field(default_factory=list)


class BackshopAgent:
    """Main agent that processes contractor messages and produces actions."""

    def __init__(self, db: Session, contractor: Contractor) -> None:
        self.db = db
        self.contractor = contractor
        self.tools: list[Tool] = []

    def register_tools(self, tools: list[Tool]) -> None:
        """Register available tools for this agent session."""
        self.tools = tools

    async def _build_system_prompt(self, message_context: str) -> str:
        """Build the full system prompt with soul + memory."""
        soul_prompt = build_soul_prompt(self.contractor)
        memory_context = await build_memory_context(
            self.db, self.contractor.id, query=message_context[:100] if message_context else None
        )
        return SYSTEM_PROMPT_TEMPLATE.format(
            contractor_name=self.contractor.name or "Contractor",
            soul_prompt=soul_prompt,
            memory_context=memory_context or "(No memories saved yet)",
        )

    async def process_message(
        self,
        message_context: str,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop."""
        system_prompt = await self._build_system_prompt(message_context)

        messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": message_context})

        tool_schemas = [tool_to_openai_schema(t) for t in self.tools] if self.tools else None

        response = await acompletion(
            model=settings.llm_model,
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            messages=messages,
            tools=tool_schemas,
            max_tokens=500,
        )

        choice = response.choices[0]
        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[dict[str, object]] = []

        # Handle tool calls
        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
            for tool_call in choice.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                tool_func = self._find_tool(tool_name)
                if tool_func:
                    try:
                        result = await tool_func(**tool_args)
                        actions_taken.append(f"Called {tool_name}")
                        tool_call_records.append(
                            {
                                "name": tool_name,
                                "args": tool_args,
                                "result": str(result),
                            }
                        )
                        if tool_name == "save_fact":
                            memories_saved.append(tool_args)
                    except Exception:
                        logger.exception("Tool call failed: %s", tool_name)
                        actions_taken.append(f"Failed: {tool_name}")

            # Get final response after tool calls
            reply_text = choice.message.content or "Done."
        else:
            reply_text = choice.message.content or ""

        return AgentResponse(
            reply_text=reply_text,
            actions_taken=actions_taken,
            memories_saved=memories_saved,
            tool_calls=tool_call_records,
        )

    def _find_tool(self, name: str) -> object | None:
        """Find a registered tool by name."""
        for tool in self.tools:
            if tool.name == name:
                return tool.function
        return None
