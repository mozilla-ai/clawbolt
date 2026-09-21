"""Wire types for the two heartbeat LLM calls.

Phase 1 asks the model whether to act and gets back a ``heartbeat_decision``
tool call; Phase 2 composes the message via ``compose_message``. The params
models double as the tool schemas the model sees, so the shapes it is asked
for and the shapes parsed back cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.agent.tools.names import ToolName


class HeartbeatDecisionParams(BaseModel):
    """Parameters for the Phase 1 heartbeat_decision tool."""

    action: Literal["skip", "run"]
    tasks: str = Field(
        default="",
        description=(
            "When action is 'run': a natural-language description of the tasks "
            "the agent should execute. Be specific about what to check or do."
        ),
    )
    reasoning: str = Field(description="Brief explanation of why this action was chosen")


HEARTBEAT_DECISION_TOOL: dict[str, Any] = {
    "name": ToolName.HEARTBEAT_DECISION,
    "description": (
        "Decide whether any heartbeat items or proactive tasks need attention right now. "
        "Choose 'skip' if nothing needs doing, or 'run' with a task description "
        "to hand off to the full agent for execution."
    ),
    "input_schema": HeartbeatDecisionParams.model_json_schema(),
}


@dataclass
class HeartbeatDecision:
    """Result of Phase 1: should the agent act?"""

    action: str  # "skip" or "run"
    tasks: str
    reasoning: str
    input_tokens: int = 0
    output_tokens: int = 0


# Legacy data structure kept for backwards compatibility with existing code
# that references HeartbeatAction (e.g. tests, return types).
class ComposeMessageParams(BaseModel):
    """Parameters for the heartbeat compose_message tool (legacy)."""

    action: Literal["send_message", "no_action"]
    message: str = Field(
        default="", description="The message to send (required if action is send_message)"
    )
    reasoning: str = Field(description="Brief explanation of why this action was chosen")
    priority: int = Field(ge=1, le=5, description="Priority level from 1 (lowest) to 5 (highest)")


COMPOSE_MESSAGE_TOOL: dict[str, Any] = {
    "name": ToolName.COMPOSE_MESSAGE,
    "description": (
        "Compose a proactive message to send to the user, or decide no message is needed."
    ),
    "input_schema": ComposeMessageParams.model_json_schema(),
}


@dataclass
class HeartbeatAction:
    action_type: str  # "send_message" or "no_action"
    message: str
    reasoning: str
    priority: int
