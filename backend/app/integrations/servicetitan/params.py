"""Pydantic parameter models for ServiceTitan tools.

Centralized so tool builders import the whole set with one line and the
agent's schema stays consistent across the integration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServiceTitanConnectParams(BaseModel):
    """Inputs the user pastes to connect a ServiceTitan tenant.

    ServiceTitan auth is OAuth 2.0 client credentials, one tenant per
    user. The tenant administrator generates the Client ID + Secret
    pair from the ServiceTitan developer portal and copies the Tenant
    ID from the Settings page; all three land here verbatim.
    """

    tenant_id: str = Field(
        description=(
            "The user's ServiceTitan Tenant ID, as shown in Settings -> "
            "Integrations -> API Application Access. Numeric string."
        ),
    )
    client_id: str = Field(
        description=(
            "The Client ID for the API application the user created in"
            " their ServiceTitan tenant. Issued together with the secret."
        ),
    )
    client_secret: str = Field(
        description=(
            "The Client Secret paired with the Client ID. Treat as a"
            " password; only shown once at creation time in ServiceTitan."
        ),
    )


class StSearchCustomersParams(BaseModel):
    """Inputs for ``st_search_customers``.

    The agent passes a single free-form query string. The tool
    decides whether to filter ServiceTitan by name or phone based on
    whether the query looks numeric. The optional ``limit`` caps the
    response so chat output stays compact; ServiceTitan's own page
    size is independent and may return more.
    """

    query: str = Field(
        description=(
            "Free-form lookup string. Treated as a name substring;"
            " if the input is mostly digits, treated as a phone-number"
            " substring instead."
        ),
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=25,
        description="Maximum number of matches to return. Defaults to 5.",
    )


class StGetCustomerParams(BaseModel):
    """Inputs for ``st_get_customer``."""

    customer_id: int = Field(
        description=(
            "The numeric ServiceTitan customer ID to look up. Usually"
            " obtained from a prior st_search_customers call."
        ),
    )


class StListAppointmentsParams(BaseModel):
    """Inputs for ``st_list_appointments``.

    All fields are optional. When ``from_date`` and ``to_date`` are
    both omitted the tool defaults to today's appointments in UTC,
    which matches the "today's dispatch view" use case in the issue.
    """

    from_date: str | None = Field(
        default=None,
        description=(
            "Inclusive lower bound on appointment start time. ISO 8601"
            " string (e.g. 2026-05-11 or 2026-05-11T08:00:00Z). Omit"
            " to default to the start of today (UTC)."
        ),
    )
    to_date: str | None = Field(
        default=None,
        description=(
            "Exclusive upper bound on appointment start time. ISO 8601"
            " string. Omit to default to the start of tomorrow (UTC)."
        ),
    )
    status: str | None = Field(
        default=None,
        description=(
            "Filter to appointments with this status. ServiceTitan"
            " values: Scheduled, Dispatched, Working, Done, Hold. Omit"
            " to return all statuses."
        ),
    )
