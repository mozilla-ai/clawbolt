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
