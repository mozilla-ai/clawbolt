"""Shared web search provider errors."""


class SearchUnavailableError(RuntimeError):
    """A search provider could not serve this request.

    Raised instead of returning an empty list so the caller can tell "this
    backend could not answer" from "the web has nothing for this query". An
    empty list means a genuine zero-result search and is reported to the model
    as such, which is a fact about the query rather than a fault to retry.
    """
