"""Pluggable general web search integration."""

from backend.app.integrations.web_search.brave import BraveSearchProvider
from backend.app.integrations.web_search.cache import SearchCache
from backend.app.integrations.web_search.errors import SearchUnavailableError
from backend.app.integrations.web_search.protocol import SearchProvider
from backend.app.integrations.web_search.render import render_records

__all__ = [
    "BraveSearchProvider",
    "SearchCache",
    "SearchProvider",
    "SearchUnavailableError",
    "render_records",
]
