from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    """Load a prompt template from a markdown file in the prompts directory."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()
