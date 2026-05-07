"""Pydantic parameter models for AppFolio Vendor Portal tools.

Kept in one module so tool builders can import the full set with one
line and so the agent's schema is centralized.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AppFolioConnectParams(BaseModel):
    magic_link: str = Field(
        description=(
            "The full magic-link URL the user pasted from their AppFolio email,"
            " e.g. 'https://vendor.appfolio.com/?magic_link_token=eyJ...'."
            " The bare token is also accepted."
        ),
    )


class AppFolioCompleteTwoFactorParams(BaseModel):
    code: str = Field(
        description=(
            "The 2FA verification code the user received via SMS or email"
            " after starting the AppFolio connection."
        ),
    )


class AppFolioListWorkOrdersParams(BaseModel):
    include_in_progress: bool = Field(
        default=True,
        description="Include work orders that are currently in progress.",
    )
    include_completed: bool = Field(
        default=False,
        description="Include completed (closed) work orders.",
    )
    include_estimates: bool = Field(
        default=True,
        description="Include work orders waiting on an estimate.",
    )
    customer_id: str = Field(
        default="",
        description=(
            "Optional AppFolio customer ID (property manager) to filter by."
            " Leave empty to merge work orders across all customers."
        ),
    )


class AppFolioSearchWorkOrdersParams(BaseModel):
    search_term: str = Field(
        description=(
            "Search query — work order number, address, unit, or any free text."
            " Matches AppFolio's universal vendor-portal search."
        ),
    )


class AppFolioGetWorkOrderParams(BaseModel):
    customer_id: str = Field(
        description="AppFolio customer (property manager) ID for this work order.",
    )
    work_order_id: str = Field(description="AppFolio work order ID.")


class AppFolioListPaymentsParams(BaseModel):
    posted_on: str = Field(
        default="",
        description=(
            "Optional ISO date (YYYY-MM-DD) to filter payments posted on or after."
            " Leave empty for all dates."
        ),
    )
    settlement_method: str = Field(
        default="",
        description=(
            "Optional settlement method filter: 'e_check', 'bill_pay_check', or"
            " 'push_to_debit'. Leave empty for all methods."
        ),
    )


class AppFolioGetProfileParams(BaseModel):
    """Empty param model — ``get_profile`` takes no arguments."""


class AppFolioAcceptWorkOrderParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID to accept.")
    notes: str = Field(
        default="",
        description="Optional acceptance notes the property manager will see.",
    )


class AppFolioScheduleWorkOrderParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID to schedule.")
    scheduled_at: str = Field(
        description=(
            "When the visit will start, as an ISO 8601 timestamp (e.g."
            " '2026-05-08T14:00:00-04:00'). Use the user's timezone."
        ),
    )
    duration_minutes: int = Field(
        default=0,
        description="Estimated visit duration in minutes (0 to omit).",
    )
    notes: str = Field(
        default="",
        description="Optional scheduling notes for the tenant or PM.",
    )


class AppFolioUpdateWorkOrderStatusParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID to update.")
    status_code: int = Field(
        description=(
            "Numeric status code AppFolio expects. Common values:"
            " 0=new, 4=in progress, 8=completed."
            " Confirm with the user when uncertain rather than guessing."
        ),
    )


class AppFolioUndoWorkOrderStatusParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID.")
    previous_status: str = Field(
        description=(
            "The status the work order should revert to. Pass the prior"
            " status code or label as returned by appfolio_get_work_order."
        ),
    )


class AppFolioListNotesParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID.")


class AppFolioAddNoteParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID to add a note to.")
    body: str = Field(description="Note text. Visible to the property manager.")
    media_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Optional list of photo references from the conversation."
            " Each entry is either an original_url from a sent image or a"
            " media handle (e.g. 'media_xxxx') returned by analyze_photo."
            " Photos are uploaded to AppFolio inline with the note."
        ),
    )


class AppFolioUpdateNoteParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID.")
    note_id: str = Field(description="AppFolio note ID to edit.")
    body: str = Field(description="Replacement note text.")
    media_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Optional list of photo references to attach, same shape as"
            " appfolio_add_note. Existing attachments are preserved."
        ),
    )


class AppFolioMessageTenantParams(BaseModel):
    work_order_id: str = Field(
        description=(
            "AppFolio work order ID. AppFolio mints an anonymized proxy"
            " number per work order, so the message routes to the right"
            " tenant without exposing the vendor's real phone number."
        ),
    )
    message: str = Field(
        description="SMS body to send to the tenant via AppFolio's proxy.",
    )
