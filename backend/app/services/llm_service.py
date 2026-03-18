"""LLM service utilities: provider enumeration and model listing."""

from __future__ import annotations

from any_llm import LLMProvider, alist_models

from backend.app.schemas import ProviderInfo

# Providers that run locally (no API key needed).
_LOCAL_PROVIDERS = {"ollama", "llamafile", "llamacpp", "lmstudio", "vllm"}

# Meta-providers that proxy to other providers and should not be directly selectable.
_HIDDEN_PROVIDERS = {"platform", "gateway"}


def get_configured_providers() -> list[ProviderInfo]:
    """Return all known providers. Actual validation happens when listing models."""
    return [
        ProviderInfo(name=p.value, local=p.value in _LOCAL_PROVIDERS)
        for p in LLMProvider
        if p.value not in _HIDDEN_PROVIDERS
    ]


async def get_models(
    provider: str,
    api_key: str | None = None,
    api_base: str | None = None,
) -> list[str]:
    """Fetch available models for a provider."""
    raw = await alist_models(provider=provider, api_key=api_key, api_base=api_base)
    return [m.id if hasattr(m, "id") else str(m) for m in raw]
