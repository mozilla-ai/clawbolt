"""Pure helpers for the agent loop.

Everything here is a free function over its arguments: overflow detection,
secret scrubbing, tool-argument coercion, and concurrency bucketing. They
live apart from ClawboltAgent so each can be exercised without standing up
an agent, and so core.py reads as the loop rather than as its utilities.
"""

from __future__ import annotations

import copy
import json
import re
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from backend.app.agent.tools.base import Tool

if TYPE_CHECKING:
    from any_llm.exceptions import AnyLLMError


# Narrow provider phrases that any-llm may not classify as context overflow.
# False positives would hide malformed requests behind a trim-and-retry cycle.
_CONTEXT_OVERFLOW_PROBES = (
    "prompt is too long",  # anthropic
    "input is too long",  # anthropic, older wording
    "context_length_exceeded",  # openai error code
    "reduce the length of the messages",  # openai
    "please reduce your prompt",  # openai
)


def _is_context_overflow(exc: AnyLLMError) -> bool:
    """True when *exc* is a context overflow any-llm failed to classify as one.

    Reads both the unified exception and its ``original_exception``, since
    any-llm keeps the raw provider error on that attribute and the useful text
    may live on either.

    A gateway that strips upstream error messages (otari replaces every provider
    400 body with a fixed string) defeats this by design: there is no signal left
    to match, so an overflow behind such a gateway still surfaces as an error.
    The proactive trim in ``process_message`` is the defense there; this is the
    reactive backstop for when the provider message survives.
    """
    parts = [exc.message, str(exc)]
    if exc.original_exception is not None:
        parts.append(str(exc.original_exception))
    haystack = " ".join(parts).lower()
    return any(probe in haystack for probe in _CONTEXT_OVERFLOW_PROBES)


# Bounded process-local cache of full API-reported prompt size per user.
# The first turn after restart falls back to the token estimate.
_LAST_INPUT_TOKENS: OrderedDict[str, int] = OrderedDict()
_LAST_INPUT_TOKENS_MAX = 1024


def _remember_input_tokens(user_id: str, tokens: int) -> None:
    """Record the latest full prompt token count for *user_id*."""
    _LAST_INPUT_TOKENS[user_id] = tokens
    _LAST_INPUT_TOKENS.move_to_end(user_id)
    while len(_LAST_INPUT_TOKENS) > _LAST_INPUT_TOKENS_MAX:
        _LAST_INPUT_TOKENS.popitem(last=False)


def _recall_input_tokens(user_id: str) -> int | None:
    """Return the last recorded prompt token count for *user_id*, if any."""
    return _LAST_INPUT_TOKENS.get(user_id)


def reset_last_input_tokens() -> None:
    """Clear the per-user input-token cache (for tests)."""
    _LAST_INPUT_TOKENS.clear()


# Patterns that commonly appear in tool exception messages and would leak
# secrets into the LLM context (and thence into provider logs and the
# `messages` table) if echoed verbatim. Add new vendors here as they show up.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "Bearer ***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_***"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"), "xox-***"),
    (re.compile(r"(?i)\b(api[_\-]?key|password|token|secret)\s*[:=]\s*\S+"), r"\1=***"),
)


def _scrub_secrets(text: str) -> str:
    """Strip well-known secret formats so we can safely echo errors to the LLM."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_ToolEntry = tuple[int, Tool, dict[str, Any]]
"""(parsed_calls index, Tool, validated args). Threaded through the per-turn
execution pipeline by validation, approval, and the parallel scheduler."""


def _normalize_tool_args(args: dict[str, Any]) -> str:
    """Canonical string form of validated tool args for duplicate detection.

    Stable across dict iteration order so the same logical call always
    hashes to the same key, regardless of how the LLM emitted the keys.
    ``default=str`` so an unexpected non-JSON value cannot crash the
    telemetry path; the goal here is a diagnostic signal, not a perfect
    encoder.
    """
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(args.items()))


def _stringify_numbers_for_string_fields(
    tool_args: dict[str, Any], exc: ValidationError
) -> dict[str, Any] | None:
    """Stringify numeric values that failed ``str`` field validation.

    LLMs emit numeric-looking values (work order numbers, street numbers,
    event titles like "20240") as JSON numbers. Pydantic v2 rejects
    ``int``/``float`` input for ``str`` fields by default, which fails the
    whole tool call even though the intent is unambiguous. Returns a copy
    of ``tool_args`` with each offending value stringified, or ``None``
    when no error is coercible (the original error should be reported).
    Booleans are left alone: ``True`` for a string field is a real bug,
    not a serialization quirk.
    """
    coerced: Any = copy.deepcopy(tool_args)
    fixed_any = False
    for error in exc.errors():
        if error.get("type") != "string_type":
            continue
        value = error.get("input")
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        container: Any = coerced
        loc = error.get("loc", ())
        try:
            for key in loc[:-1]:
                container = container[key]
            cast("dict[Any, Any]", container)[loc[-1]] = str(value)
        except (KeyError, IndexError, TypeError):
            continue
        fixed_any = True
    return coerced if fixed_any else None


def _resolve_concurrency_group(tool: Tool, validated_args: dict[str, Any]) -> str | None:
    """Resolve a tool's concurrency group for a specific call.

    ``Tool.concurrency_group`` may be a static string, a callable that
    derives a key from the call's validated args, or ``None``. The
    callable form mirrors ``ApprovalPolicy.resource_extractor`` and lets a
    single Tool route distinct calls to distinct serialization buckets
    (e.g. a workspace writer keyed by file path).
    """
    group = tool.concurrency_group
    if group is None or isinstance(group, str):
        return group
    return group(validated_args)


def _bucket_by_concurrency_group(
    approved_entries: list[_ToolEntry],
) -> list[list[tuple[int, _ToolEntry]]]:
    """Bucket approved entries into schedule units for concurrent execution.

    Each entry whose resolved concurrency group is ``None`` becomes its
    own (parallel) unit. Entries sharing a non-None group are grouped
    into a single sequential unit. Position within a non-None bucket
    follows the order entries appear in ``approved_entries``, which the
    scheduler honors when running the bucket sequentially.

    Pure-functional so the bucketing rule can be exercised without
    standing up an agent.
    """
    buckets: dict[str | None, list[tuple[int, _ToolEntry]]] = {}
    for pos, entry in enumerate(approved_entries):
        _idx, tool_obj, validated_args = entry
        key = _resolve_concurrency_group(tool_obj, validated_args)
        buckets.setdefault(key, []).append((pos, entry))

    units: list[list[tuple[int, _ToolEntry]]] = []
    for group_key, items in buckets.items():
        if group_key is None:
            units.extend([item] for item in items)
        else:
            units.append(items)
    return units
