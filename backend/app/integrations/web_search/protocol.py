"""Search provider protocol and shared result model.

The seam is deliberately one method wide. A provider takes a query string and
returns ranked results; everything else (caching, retries, formatting, the
tool surface) lives above it and is provider-agnostic, so swapping the backend
is a new module plus an entry in ``_PROVIDERS`` in ``factory.py``.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class SearchResult(BaseModel):
    """A single ranked web result, normalized across providers."""

    title: str
    url: str
    snippet: str = ""
    # Provider-reported publish or crawl date, verbatim and unparsed. Providers
    # disagree on format ("2 days ago", "March 2025", an ISO date), so this is
    # passed through to the model as a staleness signal rather than parsed into
    # a datetime we would have to guess the semantics of.
    age: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """Interface every web search backend must implement."""

    name: str
    display_name: str

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]: ...
