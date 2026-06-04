"""Pydantic parameter models for ServiceTitan tools.

Centralized so tool builders import the whole set with one line and the
agent's schema stays consistent across the integration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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


class StAddJobNoteParams(BaseModel):
    """Inputs for ``st_add_job_note``.

    Posts a free-form note to a ServiceTitan job. The note is visible
    to anyone in the tenant with access to the job, so the tool gates
    on approval before sending. ``pin_to_top`` mirrors the API's
    ``pinToTop`` flag and surfaces the note above other notes in the
    job's note feed.
    """

    job_id: int = Field(
        description=(
            "The numeric ServiceTitan job ID to attach the note to."
            " Obtain from a prior appointment lookup or list call."
        ),
    )
    text: str = Field(
        min_length=1,
        description=(
            "The note body to post on the job. Plain text. Empty or"
            " whitespace-only values are rejected."
        ),
    )
    pin_to_top: bool = Field(
        default=False,
        description=(
            "When true, ServiceTitan pins the note above other notes"
            " in the job's note feed. Defaults to false."
        ),
    )

    @field_validator("text")
    @classmethod
    def _text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value
