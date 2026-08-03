"""Shared supplier-backend errors."""


class SupplierUnavailableError(RuntimeError):
    """A supplier backend could not serve this request.

    Raised instead of returning an empty list so callers can tell "this backend
    could not answer" from "the supplier has no such product", and move on to
    the next backend in the chain. An empty list means a genuine zero-result
    search and stops the chain.
    """
