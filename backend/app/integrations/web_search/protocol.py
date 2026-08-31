"""Search provider protocol.

The seam is one method wide and deliberately does not impose a result schema.
A provider returns its own records as plain dicts, exactly as its API shaped
them, and the generic renderer in ``factory.py`` prints whatever fields are
present without knowing their names.

An earlier version mapped every provider onto a fixed model with four fields.
That is the failure mode this design exists to prevent: Brave attaches a
``product`` object carrying a real price, the fixed model had no field for it,
and the price was silently dropped on the floor. A renderer that walks the
record cannot drop a field it was never told about, so a provider adding one
starts reaching the model with no code change here.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SearchProvider(Protocol):
    """Interface every web search backend must implement.

    ``search`` returns the provider's own result records. Keys are the
    provider's, not ours; nesting is preserved. The only contract is that each
    record is a JSON-shaped dict and that a result's link, whatever the provider
    calls it, is somewhere inside it.
    """

    name: str
    display_name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int = 3,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run *query* and return the provider's own result records.

        ``freshness`` is a best-effort recency hint (``pd``/``pw``/``pm``/``py``
        for past day/week/month/year). It is on the seam rather than buried in
        one backend because recency is a property of the question, not of the
        vendor: a material price wants the last month, a building code from 2023
        wants no filter at all. A provider that cannot express it ignores it,
        which degrades to today's behavior rather than failing.
        """
        ...
