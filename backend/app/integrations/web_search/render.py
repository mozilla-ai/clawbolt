"""Generic rendering of provider result records as plain text.

Field-name agnostic on purpose. The renderer walks whatever record the provider
returned and prints every leaf it finds, so a provider that starts sending a new
field needs no change here. An earlier design mapped results onto a fixed
four-field model and silently dropped Brave's ``product.price``, which was the
one field a materials estimate actually needed.

Nothing here drops information. There is no field allowlist, no denylist, and no
truncation: a result the provider considered worth returning is rendered whole.
The two transformations applied are markup removal (in the provider, where
``<strong>`` highlight tags are stripped) and skipping keys whose value is null
or an empty string, neither of which carries anything the model could use.

The size of a response is therefore set by one number the operator can reason
about, ``WEB_SEARCH_MAX_RESULTS``, rather than by a character budget hidden in
here. A live Brave result runs roughly 2,000 characters, so the default of five
lands near 10,000. If that is ever too much, the fix is to ask for fewer
results, not to serve half of each one.
"""

from typing import Any


def _walk(value: Any, prefix: str, lines: list[str]) -> None:
    """Flatten *value* into ``key: value`` lines, dotting nested keys."""
    if isinstance(value, dict):
        for key, sub in value.items():
            _walk(sub, f"{prefix}.{key}" if prefix else str(key), lines)
        return

    if isinstance(value, list):
        for i, sub in enumerate(value):
            _walk(sub, f"{prefix}[{i}]", lines)
        return

    # Null and empty string say nothing the model can act on. Everything else,
    # including numbers and booleans, is rendered as the provider sent it.
    if value is None or value == "":
        return

    lines.append(f"   {prefix}: {value}")


def render_record(record: dict[str, Any], index: int) -> str:
    """Render one provider record as a numbered block of ``key: value`` lines."""
    lines: list[str] = []
    _walk(record, "", lines)
    body = "\n".join(lines)
    return f"[{index}]\n{body}" if body else f"[{index}]\n   (empty result)"


def render_records(records: list[dict[str, Any]]) -> str:
    """Render every record the provider returned, in order."""
    return "\n\n".join(render_record(record, i) for i, record in enumerate(records, 1))
