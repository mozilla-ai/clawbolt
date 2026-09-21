"""SQLAlchemy ORM models for clawbolt.

Split by the part of the product each table serves. Importing this
package imports every module, which is what registers the mappers on
``Base.metadata``; anything that reflects over the metadata (Alembic
autogenerate, the test-suite TRUNCATE) depends on that. Callers may
keep importing model classes from here.
"""

from __future__ import annotations

from backend.app.models.access import (
    WAITLIST_NAME_DEFAULT,
    AdminApiKey,
    AdminAuditLog,
    AllowedEmail,
    WaitlistEntry,
)
from backend.app.models.approval import (
    ApprovalEvent,
    PendingApprovalRow,
)
from backend.app.models.billing import (
    DeletedUserUsage,
    Subscription,
    UsageQuota,
)
from backend.app.models.conversation import (
    ChannelRoute,
    ChatSession,
    CompactionEvent,
    MemoryDocument,
    Message,
)
from backend.app.models.heartbeat import HeartbeatLog
from backend.app.models.infra import (
    AppSetting,
    IdempotencyKey,
    StagedMedia,
)
from backend.app.models.integrations import (
    CalendarConfig,
    OAuthToken,
    ToolConfig,
)
from backend.app.models.llm import (
    LLMEndpoint,
    LLMPayloadCapture,
    LLMUsageLog,
)
from backend.app.models.llm_eval import (
    LLMEvalRun,
    LLMEvalTurnResult,
)
from backend.app.models.moderation import ReportedConversation
from backend.app.models.types import EncryptedString
from backend.app.models.user import (
    User,
    UserPermissionSet,
)

__all__ = [
    "WAITLIST_NAME_DEFAULT",
    "AdminApiKey",
    "AdminAuditLog",
    "AllowedEmail",
    "AppSetting",
    "ApprovalEvent",
    "CalendarConfig",
    "ChannelRoute",
    "ChatSession",
    "CompactionEvent",
    "DeletedUserUsage",
    "EncryptedString",
    "HeartbeatLog",
    "IdempotencyKey",
    "LLMEndpoint",
    "LLMEvalRun",
    "LLMEvalTurnResult",
    "LLMPayloadCapture",
    "LLMUsageLog",
    "MemoryDocument",
    "Message",
    "OAuthToken",
    "PendingApprovalRow",
    "ReportedConversation",
    "StagedMedia",
    "Subscription",
    "ToolConfig",
    "UsageQuota",
    "User",
    "UserPermissionSet",
    "WaitlistEntry",
]
