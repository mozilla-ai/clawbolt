"""Pluggable general web search integration."""

from backend.app.integrations.web_search.brave import BraveSearchProvider
from backend.app.integrations.web_search.cache import SearchCache
from backend.app.integrations.web_search.errors import SearchUnavailableError
from backend.app.integrations.web_search.protocol import SearchProvider, SearchResult

__all__ = [
    "BraveSearchProvider",
    "SearchCache",
    "SearchProvider",
    "SearchResult",
    "SearchUnavailableError",
]
