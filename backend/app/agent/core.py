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

MAX_TOOL_ROUNDS = 5
CONTEXT_QUERY_MAX_LENGTH = 100

SYSTEM_PROMPT_TEMPLATE = """You are Backshop, an AI assistant for solo contractors.

## About {contractor_name}
{soul_prompt}

## Your Memory
{memory_context}

## Instructions
- Be concise and practical. Contractors are busy.
- When you learn new information (rates, clients, preferences), save it using the save_fact tool.
- When asked for an estimate, gather the details and use the appropriate tools.
- Always be helpful, friendly, and professional.
- Keep SMS replies under 160 characters when possible (single SMS segment).

## Recall Behavior
When the contractor asks a question about their business, clients, or past work:
1. Use recall_facts to search your memory for relevant information.
2. If you find relevant facts, use them to answer clearly and concisely.
3. If you don't find anything, say so honestly — don't make things up.
4. If the question is about general knowledge (not their specific business), answer from your training.
5. For "what do you know about me?" questions, summarize key facts by category.
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
            self.db,
            self.contractor.id,
            query=message_context[:CONTEXT_QUERY_MAX_LENGTH] if message_context else None,
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
        system_prompt_override: str | None = None,
        temperature: float | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop."""
        system_prompt = system_prompt_override or await self._build_system_prompt(message_context)

        messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": message_context})

        tool_schemas = [tool_to_openai_schema(t) for t in self.tools] if self.tools else None

        llm_kwargs: dict[str, object] = {}
        if settings.llm_provider == "openai":
            llm_kwargs["user"] = str(self.contractor.id)
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[dict[str, object]] = []
        reply_text = ""

        for _round in range(MAX_TOOL_ROUNDS):
            response = await acompletion(
                model=settings.llm_model,
                provider=settings.llm_provider,
                api_base=settings.llm_api_base,
                messages=messages,
                tools=tool_schemas,
                max_tokens=settings.llm_max_tokens_agent,
                **llm_kwargs,
            )

            choice = response.choices[0]

            if not getattr(choice.message, "tool_calls", None):
                reply_text = choice.message.content or ""
                break

            # Append the assistant message (with tool_calls) to conversation
            messages.append(choice.message.model_dump())

            tool_results: list[dict[str, str]] = []
            for tool_call in choice.message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    tool_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed tool arguments for %s: %s",
                        tool_name,
                        tool_call.function.arguments[:200],
                    )
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Error: malformed arguments for {tool_name}",
                        }
                    )
                    actions_taken.append(f"Failed: {tool_name} (bad args)")
                    continue

                tool_func = self._find_tool(tool_name)
                result_str = ""
                if tool_func:
                    try:
                        result = await tool_func(**tool_args)
                        result_str = str(result)
                        actions_taken.append(f"Called {tool_name}")
                        tool_call_records.append(
                            {
                                "name": tool_name,
                                "args": tool_args,
                                "result": result_str,
                            }
                        )
                        if tool_name == "save_fact":
                            memories_saved.append(tool_args)
                    except Exception:
                        logger.exception("Tool call failed: %s", tool_name)
                        result_str = f"Error: tool {tool_name} failed"
                        actions_taken.append(f"Failed: {tool_name}")
                else:
                    result_str = f"Error: unknown tool {tool_name}"

                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    }
                )

            messages.extend(tool_results)
        else:
            # Max rounds reached — use last response content
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
