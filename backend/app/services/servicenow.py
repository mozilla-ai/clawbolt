"""ServiceNow Table API service.

Provides async methods for interacting with ServiceNow's Field Service
Management tables (wm_order, wm_task, time_card) via the Table API.

All table names are hardcoded in each method to prevent LLM-controlled
input from accessing arbitrary tables.
"""

from __future__ import annotations

import logging
import re

import httpx

from backend.app.services.servicenow_models import (
    TimeCard,
    WorkOrder,
    WorkOrderTask,
)

logger = logging.getLogger(__name__)

# Regex for validating ServiceNow instance URLs.
_INSTANCE_URL_RE = re.compile(
    r"^https://[\w-]+\.(service-now|servicenow)\.com$",
    re.IGNORECASE,
)


def validate_instance_url(url: str) -> str:
    """Validate and normalize a ServiceNow instance URL.

    Strips trailing slashes and verifies the URL matches the expected
    ``*.service-now.com`` or ``*.servicenow.com`` pattern to prevent
    credential leakage to attacker-controlled servers.

    Raises ``ValueError`` if the URL is invalid.
    """
    url = url.rstrip("/")
    if not _INSTANCE_URL_RE.match(url):
        raise ValueError(
            f"Invalid ServiceNow instance URL: {url!r}. "
            "Expected format: https://<instance>.service-now.com"
        )
    return url


class ServiceNowService:
    """Client for the ServiceNow Table API.

    Requires a Bearer access token and the customer's instance URL.
    """

    def __init__(
        self,
        access_token: str,
        instance_url: str,
        sys_user_id: str = "",
    ) -> None:
        if not access_token:
            raise ValueError("ServiceNow access token is required")
        self._access_token = access_token
        self._instance_url = validate_instance_url(instance_url)
        self.sys_user_id = sys_user_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @property
    def _api_base(self) -> str:
        return f"{self._instance_url}/api/now/table"

    # -- Read operations -------------------------------------------------------

    async def validate_token(self) -> bool:
        """Validate the access token by fetching a single sys_user record."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._api_base}/sys_user",
                params={"sysparm_limit": "1"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return True

    async def resolve_current_user(self) -> str:
        """Resolve the current OAuth user's sys_id.

        Queries the sys_user table with the ``sysparm_limit=1`` and the
        token's identity. Returns the sys_id string, or empty string if
        the user cannot be resolved.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._instance_url}/api/now/myuser",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                # Fallback: try table API
                resp = await client.get(
                    f"{self._api_base}/sys_user",
                    params={"sysparm_limit": "1", "sysparm_fields": "sys_id,user_name"},
                    headers=self._headers(),
                )
            resp.raise_for_status()
            data = resp.json()
            # /api/now/myuser returns {"result": {"sys_id": "..."}}
            result = data.get("result")
            if isinstance(result, dict):
                return result.get("sys_id", "")
            # Table API returns {"result": [{...}]}
            if isinstance(result, list) and result:
                return result[0].get("sys_id", "")
            return ""

    async def list_work_orders(
        self,
        *,
        assigned_to: str = "",
        state: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> list[WorkOrder]:
        """List work orders, optionally filtered by assignee and state."""
        effective_assigned_to = assigned_to or self.sys_user_id
        query_parts: list[str] = []
        if effective_assigned_to:
            query_parts.append(f"assigned_to={effective_assigned_to}")
        if state:
            query_parts.append(f"state={state}")

        params: dict[str, str] = {
            "sysparm_display_value": "all",
            "sysparm_limit": str(min(limit, 50)),
            "sysparm_offset": str(offset),
            "sysparm_fields": (
                "sys_id,number,short_description,state,priority,"
                "assigned_to,location,opened_at,closed_at,work_start,work_end"
            ),
        }
        if query_parts:
            params["sysparm_query"] = "^".join(query_parts)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self._api_base}/wm_order",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
            return [WorkOrder.model_validate(r) for r in records]

    async def get_work_order(self, sys_id: str) -> WorkOrder:
        """Get a single work order by sys_id."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self._api_base}/wm_order/{sys_id}",
                params={"sysparm_display_value": "all"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return WorkOrder.model_validate(resp.json().get("result", {}))

    async def list_tasks(
        self,
        *,
        work_order_id: str = "",
        state: str = "",
        limit: int = 25,
    ) -> list[WorkOrderTask]:
        """List work order tasks, optionally filtered by work order and state."""
        query_parts: list[str] = []
        if work_order_id:
            query_parts.append(f"work_order={work_order_id}")
        if state:
            query_parts.append(f"state={state}")

        params: dict[str, str] = {
            "sysparm_display_value": "all",
            "sysparm_limit": str(min(limit, 50)),
            "sysparm_fields": (
                "sys_id,number,short_description,state,assigned_to,"
                "work_order,work_start,work_end,work_notes"
            ),
        }
        if query_parts:
            params["sysparm_query"] = "^".join(query_parts)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self._api_base}/wm_task",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
            return [WorkOrderTask.model_validate(r) for r in records]

    async def search_work_orders(
        self,
        query: str,
        limit: int = 25,
    ) -> list[WorkOrder]:
        """Search work orders by text (short_description or number).

        Queries are built server-side to prevent injection of arbitrary
        ServiceNow encoded query operators.
        """
        safe_query = query.replace("^", "").replace("\n", " ").strip()
        if not safe_query:
            return []

        params: dict[str, str] = {
            "sysparm_display_value": "all",
            "sysparm_limit": str(min(limit, 50)),
            "sysparm_query": (f"short_descriptionLIKE{safe_query}^ORnumberLIKE{safe_query}"),
            "sysparm_fields": (
                "sys_id,number,short_description,state,priority,"
                "assigned_to,location,opened_at,closed_at"
            ),
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self._api_base}/wm_order",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
            return [WorkOrder.model_validate(r) for r in records]

    # -- Write operations ------------------------------------------------------

    async def update_task(
        self,
        sys_id: str,
        *,
        state: str = "",
        work_notes: str = "",
    ) -> WorkOrderTask:
        """Update a work order task's state and/or work notes."""
        body: dict[str, str] = {}
        if state:
            body["state"] = state
        if work_notes:
            body["work_notes"] = work_notes
        if not body:
            raise ValueError("At least one of state or work_notes must be provided")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{self._api_base}/wm_task/{sys_id}",
                json=body,
                headers=self._headers(),
                params={"sysparm_display_value": "all"},
            )
            resp.raise_for_status()
            return WorkOrderTask.model_validate(resp.json().get("result", {}))

    async def add_work_order_note(self, sys_id: str, note: str) -> WorkOrder:
        """Add a work note to a work order (hardcoded table: wm_order)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{self._api_base}/wm_order/{sys_id}",
                json={"work_notes": note},
                headers=self._headers(),
                params={"sysparm_display_value": "all"},
            )
            resp.raise_for_status()
            return WorkOrder.model_validate(resp.json().get("result", {}))

    async def add_task_note(self, sys_id: str, note: str) -> WorkOrderTask:
        """Add a work note to a work order task (hardcoded table: wm_task)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{self._api_base}/wm_task/{sys_id}",
                json={"work_notes": note},
                headers=self._headers(),
                params={"sysparm_display_value": "all"},
            )
            resp.raise_for_status()
            return WorkOrderTask.model_validate(resp.json().get("result", {}))

    async def create_time_card(
        self,
        *,
        task_id: str,
        hours: float,
        date: str,
        category: str = "labor",
    ) -> TimeCard:
        """Create a time card entry for a work order task."""
        body = {
            "task": task_id,
            "total": str(hours),
            "date": date,
            "category": category,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._api_base}/time_card",
                json=body,
                headers=self._headers(),
                params={"sysparm_display_value": "all"},
            )
            resp.raise_for_status()
            return TimeCard.model_validate(resp.json().get("result", {}))
