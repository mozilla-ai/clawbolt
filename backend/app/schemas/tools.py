"""Tool configuration, OAuth status, and integration connection payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class SubToolEntryResponse(BaseModel):
    name: str
    description: str
    permission_level: str = "always"
    hidden_in_permissions: bool = False


class ToolConfigEntryResponse(BaseModel):
    name: str
    description: str
    category: str
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool
    configured: bool = True
    auth_message: str = ""
    # Name of the OAuth integration backing this tool (as registered in
    # ``backend.app.services.oauth``), or empty when the tool is not
    # OAuth-backed. Lets the Settings UI render Connect/Disconnect buttons
    # without hand-maintaining a factory-to-OAuth map per integration.
    oauth_name: str = ""
    # When ``True``, the backend refuses to disable this tool (mirrors
    # ``ToolFactory.dashboard_always_enabled``). The Settings UI uses this
    # to hide the enable/disable toggle so the user does not see a switch
    # that silently bounces back. Decoupled from ``category`` so future
    # purely-internal categories cannot accidentally hide the toggle for
    # always-on OAuth tools (Google Drive).
    always_enabled: bool = False
    # Integration key for tools that connect by submitting secrets through a
    # web form (ServiceTitan client credentials, AppFolio magic link) rather
    # than an OAuth redirect. Empty when the tool is not form-connected. The
    # Settings UI keys off this to render the right credential form instead of
    # an OAuth Connect button, so these secrets never travel through a chat
    # thread (issue #1337).
    connect_form: str = ""
    sub_tools: list[SubToolEntryResponse] = Field(default_factory=list)


class ToolConfigResponse(BaseModel):
    tools: list[ToolConfigEntryResponse]


class SubToolPermissionUpdate(BaseModel):
    """Per-sub-tool permission override sent by the Settings UI.

    ``permission_level`` is the new value: ``"always"`` (auto-run),
    ``"ask"`` (prompt before running), or ``"never"`` (hide from the
    LLM schema). Sub-tools omitted from the update list keep their
    current stored level.
    """

    name: str
    permission_level: str


class ToolConfigUpdateEntry(BaseModel):
    name: str
    enabled: bool
    sub_tools: list[SubToolPermissionUpdate] | None = None


class ToolConfigUpdate(BaseModel):
    tools: list[ToolConfigUpdateEntry]


class OAuthStatusEntry(BaseModel):
    integration: str
    configured: bool
    connected: bool


class OAuthStatusResponse(BaseModel):
    integrations: list[OAuthStatusEntry]


class OAuthAuthorizeResponse(BaseModel):
    url: str
    integration: str


class ServiceTitanConnectRequest(BaseModel):
    """The three values from ServiceTitan's API Application Access page.

    ``client_secret`` is a ``SecretStr`` so it is masked in logs/reprs and
    marked write-only in the OpenAPI schema; the value still arrives as a
    plain JSON string from the client.
    """

    tenant_id: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    client_secret: SecretStr = Field(..., min_length=1)


class AppFolioConnectRequest(BaseModel):
    """A pasted AppFolio magic link (full URL or the bare token).

    ``magic_link`` is a single-use secret, so it is a ``SecretStr`` (masked
    in logs/reprs, write-only in the OpenAPI schema).
    """

    magic_link: SecretStr = Field(..., min_length=1)


class IntegrationConnectionResponse(BaseModel):
    """Result of a connect/disconnect on a web-form integration."""

    integration: str
    connected: bool


class CalendarListEntry(BaseModel):
    id: str
    summary: str
    primary: bool = False
    access_role: str = ""


class CalendarListResponse(BaseModel):
    calendars: list[CalendarListEntry]


class CalendarConfigEntry(BaseModel):
    calendar_id: str
    display_name: str
    disabled_tools: list[str] = Field(default_factory=list)
    access_role: str = ""


class CalendarConfigResponse(BaseModel):
    calendars: list[CalendarConfigEntry]


class CalendarConfigUpdate(BaseModel):
    calendars: list[CalendarConfigEntry]
