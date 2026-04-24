"""Pydantic models for ServiceNow Table API responses.

ServiceNow's Table API returns reference fields in different shapes depending
on the ``sysparm_display_value`` parameter:

- ``false`` (default): raw sys_id string
- ``true``: display value string only
- ``all``: ``{"value": "<sys_id>", "display_value": "<label>", "link": "<url>"}``

We use ``sysparm_display_value=all`` so reference fields are always returned
as ``SNDisplayValue`` objects, giving both the programmatic sys_id and the
human-readable label in a single call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SNDisplayValue(BaseModel):
    """A ServiceNow reference or choice field returned with display_value=all.

    When the API returns a reference or choice field with
    ``sysparm_display_value=all``, each field is an object with the raw
    value, a human-readable display string, and an optional REST link.
    """

    value: str = ""
    display_value: str = ""
    link: str = ""

    def __str__(self) -> str:
        return self.display_value or self.value


class WorkOrder(BaseModel):
    """ServiceNow FSM work order (table: wm_order)."""

    sys_id: str = ""
    number: str = ""
    short_description: str = ""
    description: str = ""
    state: SNDisplayValue = Field(default_factory=SNDisplayValue)
    priority: SNDisplayValue = Field(default_factory=SNDisplayValue)
    assigned_to: SNDisplayValue = Field(default_factory=SNDisplayValue)
    location: SNDisplayValue = Field(default_factory=SNDisplayValue)
    opened_at: str = ""
    closed_at: str = ""
    work_start: str = ""
    work_end: str = ""

    model_config = {"extra": "allow"}


class WorkOrderTask(BaseModel):
    """ServiceNow FSM work order task (table: wm_task)."""

    sys_id: str = ""
    number: str = ""
    short_description: str = ""
    description: str = ""
    state: SNDisplayValue = Field(default_factory=SNDisplayValue)
    assigned_to: SNDisplayValue = Field(default_factory=SNDisplayValue)
    work_order: SNDisplayValue = Field(default_factory=SNDisplayValue)
    work_start: str = ""
    work_end: str = ""
    work_notes: str = ""

    model_config = {"extra": "allow"}


class TimeCard(BaseModel):
    """ServiceNow time card (table: time_card)."""

    sys_id: str = ""
    task: SNDisplayValue = Field(default_factory=SNDisplayValue)
    user: SNDisplayValue = Field(default_factory=SNDisplayValue)
    total: str = ""  # hours as string in SN
    date: str = ""
    state: str = ""
    category: str = ""

    model_config = {"extra": "allow"}
