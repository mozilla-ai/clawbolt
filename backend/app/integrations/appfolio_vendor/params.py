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
