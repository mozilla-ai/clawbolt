"""Provider discovery, model configuration, endpoints, and usage accounting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.common import ReasoningEffort


class ProviderInfo(BaseModel):
    name: str
    local: bool


class ModelConfigResponse(BaseModel):
    llm_endpoint: str
    llm_provider: str
    llm_model: str
    llm_api_base: str | None
    vision_model: str
    vision_endpoint: str
    vision_provider: str
    heartbeat_model: str
    heartbeat_endpoint: str
    heartbeat_provider: str
    compaction_model: str
    compaction_endpoint: str
    compaction_provider: str
    reasoning_effort: str


class ModelConfigUpdate(BaseModel):
    llm_endpoint: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_base: str | None = None
    vision_model: str | None = None
    vision_endpoint: str | None = None
    vision_provider: str | None = None
    heartbeat_model: str | None = None
    heartbeat_endpoint: str | None = None
    heartbeat_provider: str | None = None
    compaction_model: str | None = None
    compaction_endpoint: str | None = None
    compaction_provider: str | None = None
    reasoning_effort: ReasoningEffort | None = None


class LLMEndpointItem(BaseModel):
    """One configured endpoint. ``api_key`` is never returned in cleartext."""

    name: str
    dialect: str
    base_url: str
    api_key_set: bool = False
    cache_control: str = "auto"
    reasoning: str = "auto"
    pricing: str = "auto"
    notes: str = ""


class LLMEndpointListResponse(BaseModel):
    items: list[LLMEndpointItem]


class LLMEndpointTestRequest(BaseModel):
    """Optional model to probe with. Empty asks the endpoint what it serves."""

    model: str = Field(default="", max_length=128)


class LLMEndpointTestResult(BaseModel):
    """Outcome of one probe call against a configured endpoint."""

    ok: bool
    model: str = ""
    """The model actually probed, which may have been chosen for the caller."""
    detail: str = ""
    """Empty on success; the provider's own error text otherwise."""
    latency_ms: float = 0.0
    reasoning: str = ""
    """How the probe asked for reasoning, in words. The probe always carries a
    tool, so there is nothing to report about that."""


class LLMEndpointUpsert(BaseModel):
    """Create or replace one endpoint.

    ``api_key`` accepts the ``MASK`` sentinel to mean "leave the stored key
    alone", so the form can be re-submitted without the operator retyping a
    secret the UI never showed them.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    dialect: str = Field(min_length=1, max_length=64)
    base_url: str = Field(default="", max_length=512)
    api_key: str | None = None
    cache_control: Literal["auto", "always", "never"] = "auto"
    reasoning: Literal["auto", "thinking", "effort", "none"] = "auto"
    pricing: Literal["auto", "unpriced"] = "auto"
    notes: str = Field(default="", max_length=256)


class LLMUsageByPurpose(BaseModel):
    purpose: str
    call_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost: float


class LLMUsageSummary(BaseModel):
    total_calls: int
    total_tokens: int
    total_cost: float
    by_purpose: list[LLMUsageByPurpose]
    # Calls whose cost could not be computed, so ``total_cost`` is a lower
    # bound rather than the spend. Non-zero when traffic went through an
    # endpoint marked unpriced, or through a model no price list knows.
    unpriced_calls: int = 0


class AdminLLMConfigResponse(BaseModel):
    """Global default LLM (used when a user has no per-user override)."""

    llm_endpoint: str = ""
    llm_provider: str
    llm_model: str
    llm_api_base: str | None = None
    reasoning_effort: str = "auto"


class AdminLLMConfigUpdate(BaseModel):
    """All fields optional. Pass only what you want to change."""

    llm_endpoint: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_base: str | None = None
    reasoning_effort: ReasoningEffort | None = None


class AdminUserLLMOverrideResponse(BaseModel):
    """Per-user override values plus the resolved (effective) values.

    Empty ``llm_provider_override`` / ``llm_model_override`` mean "fall
    back to the global default". The effective fields show what the
    agent will actually use, factoring in fallbacks.
    """

    user_id: str
    llm_endpoint_override: str = ""
    llm_provider_override: str
    llm_model_override: str
    effective_llm_endpoint: str = ""
    effective_llm_provider: str
    effective_llm_model: str


class AdminUserLLMOverrideUpdate(BaseModel):
    """Pass empty strings to clear an override and fall back to the global default."""

    llm_endpoint_override: str | None = None
    llm_provider_override: str | None = None
    llm_model_override: str | None = None


class AdminUserPlanUpdate(BaseModel):
    """Set a user's subscription plan. Must be a key in ``billing.plans.PLANS``."""

    plan: str


class AdminUserPlanResponse(BaseModel):
    """Resulting plan plus the active month's caps after the change."""

    user_id: str
    plan: str
    messages_limit: int
    tokens_limit: int


class AdminLLMProvider(BaseModel):
    """One entry in the admin provider list."""

    name: str
    local: bool  # True for providers like ollama/llamafile that need no API key


class AdminLLMProvidersResponse(BaseModel):
    providers: list[AdminLLMProvider]


class AdminLLMModelsResponse(BaseModel):
    """Structured result of an ``alist_models`` call, with failure context.

    The admin UI uses this to decide between:
      - rendering a real ``<select>`` (``models`` non-empty)
      - rendering an inline error + text-input fallback (``error`` set)
      - rendering a "this provider does not support listing models"
        notice + text-input fallback (``supports_listing == False``)
    """

    provider: str
    models: list[str]
    supports_listing: bool
    error: str | None = None


class LLMUsageLogItem(BaseModel):
    id: int
    timestamp: str
    """Named endpoint that served the call, empty for a bare provider."""
    endpoint: str = ""
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: str
    """Zero and meaningless when ``pricing_available`` is false."""
    pricing_available: bool = True
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None


class LLMUsageLogListResponse(BaseModel):
    total: int
    items: list[LLMUsageLogItem]
