"""GDPR data export: collect all user data into a JSON-serializable dict."""

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.file_store import (
    HeartbeatStore,
    get_memory_store,
    get_session_store,
)
from backend.app.models import LLMUsageLog, Subscription, UsageQuota, User

logger = logging.getLogger(__name__)


def _isoformat(dt: datetime.datetime | str | None) -> str | None:
    """Safely convert a datetime or ISO string to ISO format string."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


async def export_user_data(db: AsyncSession, user: User) -> dict:
    """Collect all data associated with a user for GDPR export."""
    uid = user.id

    # Profile from UserData
    profile = {
        "id": user.id,
        "user_id": user.user_id,
        "phone": user.phone,
        "soul_text": user.soul_text,
        "user_text": user.user_text,
        "heartbeat_text": user.heartbeat_text,
        "timezone": user.timezone,
        "preferred_channel": user.preferred_channel,
        "channel_identifier": user.channel_identifier,
        "onboarding_complete": user.onboarding_complete,
        "is_active": user.is_active,
        "heartbeat_opt_in": user.heartbeat_opt_in,
        "heartbeat_frequency": user.heartbeat_frequency,
        "created_at": _isoformat(user.created_at),
        "updated_at": _isoformat(user.updated_at),
    }

    # Memories from DB-backed store
    memory_store = get_memory_store(uid)
    memories = await memory_store.read_memory_async()

    # Sessions and messages from DB-backed store
    session_store = get_session_store(uid)
    all_sessions = await session_store.list_sessions()
    conversations = []
    for session in all_sessions:
        messages = [
            {
                "direction": msg.direction,
                "body": msg.body,
                "created_at": _isoformat(msg.timestamp),
            }
            for msg in session.messages
        ]
        conversations.append(
            {
                "session_id": session.session_id,
                "started_at": _isoformat(session.created_at),
                "last_message_at": _isoformat(session.last_message_at),
                "messages": messages,
            }
        )

    # Heartbeat data from store
    heartbeat_store = HeartbeatStore(uid)
    heartbeat_text = await heartbeat_store.read_heartbeat_md_async()

    # LLM usage from DB
    llm_logs = (
        (
            await db.execute(
                select(LLMUsageLog)
                .where(LLMUsageLog.user_id == uid)
                .order_by(LLMUsageLog.created_at.desc())
                .limit(10_000)
            )
        )
        .scalars()
        .all()
    )
    llm_usage = [
        {
            "provider": log.provider,
            "model": log.model,
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "total_tokens": log.total_tokens,
            "cost": str(log.cost),
            "purpose": log.purpose,
            "created_at": _isoformat(log.created_at),
        }
        for log in llm_logs
    ]

    # Subscription
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == uid))
    ).scalar_one_or_none()
    subscription = None
    if sub:
        subscription = {
            "plan": sub.plan,
            "status": sub.status,
            "created_at": _isoformat(sub.created_at),
        }

    # Usage quotas
    quotas = [
        {
            "period_start": _isoformat(q.period_start),
            "messages_used": q.messages_used,
            "messages_limit": q.messages_limit,
            "tokens_used": q.tokens_used,
            "tokens_limit": q.tokens_limit,
        }
        for q in (await db.execute(select(UsageQuota).where(UsageQuota.user_id == uid)))
        .scalars()
        .all()
    ]

    return {
        "profile": profile,
        "memories": memories,
        "conversations": conversations,
        "heartbeat_text": heartbeat_text,
        "llm_usage": llm_usage,
        "subscription": subscription,
        "usage_quotas": quotas,
    }
