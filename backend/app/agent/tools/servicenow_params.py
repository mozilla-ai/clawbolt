"""Pydantic parameter models for ServiceNow FSM tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Valid work order task states in ServiceNow FSM.
WOTaskState = Literal[
    "Pending Dispatch",
    "Assigned",
    "Accepted",
    "Work In Progress",
    "Closed Complete",
    "Closed Incomplete",
    "Cancelled",
]


class ListWorkOrdersParams(BaseModel):
    """Parameters for listing work orders."""

    assigned_to: str = Field(
        default="",
        description=(
            "ServiceNow sys_id of the assigned user. Leave empty to default to the current user."
        ),
    )
    state: str = Field(
        default="",
        description="Filter by work order state (e.g. 'Assigned', 'Work In Progress').",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50).",
    )


class GetWorkOrderParams(BaseModel):
    """Parameters for getting a single work order."""

    sys_id: str = Field(
        description="The sys_id of the work order to retrieve.",
        max_length=32,
    )


class ListTasksParams(BaseModel):
    """Parameters for listing work order tasks."""

    work_order_id: str = Field(
        default="",
        description="Filter tasks by work order sys_id. Leave empty for all tasks.",
        max_length=32,
    )
    state: str = Field(
        default="",
        description="Filter by task state.",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50).",
    )


class UpdateTaskParams(BaseModel):
    """Parameters for updating a work order task."""

    sys_id: str = Field(
        description="The sys_id of the task to update.",
        max_length=32,
    )
    state: WOTaskState | None = Field(
        default=None,
        description=(
            "New task state. Valid values: Pending Dispatch, Assigned, "
            "Accepted, Work In Progress, Closed Complete, Closed Incomplete, Cancelled."
        ),
    )
    work_notes: str = Field(
        default="",
        description="Work note to add to the task.",
    )


class AddWorkOrderNoteParams(BaseModel):
    """Parameters for adding a work note to a work order."""

    sys_id: str = Field(
        description="The sys_id of the work order.",
        max_length=32,
    )
    note: str = Field(
        description="The work note text to add.",
    )


class AddTaskNoteParams(BaseModel):
    """Parameters for adding a work note to a task."""

    sys_id: str = Field(
        description="The sys_id of the task.",
        max_length=32,
    )
    note: str = Field(
        description="The work note text to add.",
    )


class LogTimeParams(BaseModel):
    """Parameters for logging time to a work order task."""

    task_id: str = Field(
        description="The sys_id of the task to log time against.",
        max_length=32,
    )
    hours: float = Field(
        description="Number of hours worked.",
        gt=0,
        le=24,
    )
    date: str = Field(
        description="Date of work in YYYY-MM-DD format.",
    )
    category: str = Field(
        default="labor",
        description="Time category (e.g. 'labor', 'travel').",
    )


class SearchParams(BaseModel):
    """Parameters for searching work orders by text."""

    query: str = Field(
        description="Search text to match against work order descriptions or numbers.",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50).",
    )
