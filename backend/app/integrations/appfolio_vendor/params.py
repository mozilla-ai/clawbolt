"""Pydantic parameter models for AppFolio Vendor Portal tools.

Kept in one module so tool builders can import the full set with one
line and so the agent's schema is centralized.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AppFolioConnectParams(BaseModel):
    magic_link: str = Field(
        description=(
            "The magic-link token the user pasted from their AppFolio email"
            " (the value after 'magic_link_token=' in the URL, e.g. 'eyJ...')."
            " A full URL is also accepted, but ask the user to paste only the"
            " token: iMessage and other SMS clients strip query params from"
            " pasted links."
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


class AppFolioScheduleWorkOrderParams(BaseModel):
    work_order_id: str = Field(description="AppFolio work order ID to schedule.")
    time_slot_id: str = Field(
        description=(
            "Pre-defined time-slot ID published by the property manager."
            " AppFolio's vendor portal does not accept arbitrary timestamps;"
            " vendors pick from offered slots. Slot IDs come from the work"
            " order's ``time_slots`` (or from ``appfolio_get_work_order``)."
        ),
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


class AppFolioInvoiceLineItem(BaseModel):
    description: str = Field(description="Line-item description (e.g. 'Labor: 4hr').")
    quantity: float = Field(default=1.0, description="Quantity (decimal supported).")
    amount: float = Field(
        description=(
            "Per-unit price in dollars. The line total (quantity x amount)"
            " is what AppFolio actually stores, so a labor line of 5 hours"
            " at $55/hr should be sent as quantity=5, amount=55, not"
            " quantity=1, amount=275."
        ),
    )


class AppFolioCreateInvoiceParams(BaseModel):
    customer_id: str = Field(
        description="AppFolio customer (property manager) ID for this invoice.",
    )
    work_order_id: str = Field(description="Work order ID this invoice bills against.")
    line_items: list[AppFolioInvoiceLineItem] = Field(
        description=(
            "List of line items for the invoice. Each entry has description, quantity, and amount."
        ),
    )
    reference_number: str = Field(
        default="",
        description=(
            "Optional vendor-side reference number to print on the invoice."
            " The SPA defaults this to '<workOrderNumber>-<sequence>'; leave"
            " empty to let AppFolio generate one."
        ),
    )
    media_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Optional photo or document references from the conversation"
            " to attach as supporting evidence (same shape as appfolio_add_note)."
        ),
    )


class AppFolioUploadInvoicePdfParams(BaseModel):
    customer_id: str = Field(
        description="AppFolio customer (property manager) ID for this invoice.",
    )
    work_order_id: str = Field(description="Work order ID this invoice bills against.")
    media_refs: list[str] = Field(
        description=(
            "Photo or PDF references from the conversation. Each entry is"
            " an original_url or a media handle. AppFolio uploads them as"
            " a single invoice document."
        ),
    )
    reference_number: str = Field(
        default="",
        description="Optional vendor-side reference number printed on the invoice.",
    )


class AppFolioGetEstimateParams(BaseModel):
    estimate_id: str = Field(description="AppFolio estimate ID.")


class AppFolioUpdateEstimateParams(BaseModel):
    estimate_id: str = Field(description="AppFolio estimate ID to update.")
    amount: float | None = Field(
        default=None,
        description="Updated total amount in dollars. Omit to leave unchanged.",
    )
    description: str = Field(
        default="",
        description="Updated estimate description. Empty leaves it unchanged.",
    )
    notes: str = Field(
        default="",
        description="Optional vendor notes for the property manager.",
    )
