"""Ensure .env.example stays in sync with the Settings class."""

import re
from pathlib import Path

from backend.app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"


def _parse_env_example_keys() -> set[str]:
    """Return every variable name mentioned in .env.example (commented or not)."""
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        # Match both "VAR=value" and "# VAR=value" lines
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def test_all_settings_fields_documented_in_env_example() -> None:
    """Every field in Settings must appear in .env.example."""
    settings_keys = {field.upper() for field in Settings.model_fields}
    env_keys = _parse_env_example_keys()

    missing = settings_keys - env_keys
    assert not missing, (
        f"Settings fields missing from .env.example: {sorted(missing)}. "
        "Add them (commented out is fine) so users can discover all options."
    )
