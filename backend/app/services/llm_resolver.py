"""Per-user LLM override resolver registered with the agent.

The agent calls the registered resolver from ``run_agent_step`` (once
per inbound message). The resolver returns ``(provider, model)`` for the
user, or ``None`` when the user has no override and the agent should
fall back to ``settings.llm_*`` globals.

Either field of the returned tuple can be empty; OSS treats an empty
field as "use the global default for this field". So an admin can pin a
user to a specific model on the global provider by setting
``llm_model_override`` only.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from backend.app.database import db_session_async
from backend.app.models import Subscription
from backend.app.services.llm_service import set_user_llm_resolver

logger = logging.getLogger(__name__)


async def user_llm_override_resolver(user_id: str) -> tuple[str, str] | None:
    """Look up the per-user (provider, model) override, or None when absent."""
    async with db_session_async() as db:
        sub = (
            await db.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).scalar_one_or_none()
        if sub is None:
            return None
        if not sub.llm_provider_override and not sub.llm_model_override:
            return None
        return (sub.llm_provider_override, sub.llm_model_override)


def install_user_llm_resolver() -> None:
    """Register ``user_llm_override_resolver`` with the agent."""
    set_user_llm_resolver(user_llm_override_resolver)
    logger.info("Per-user LLM override resolver installed")
