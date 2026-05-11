"""ServiceTitan read tools.

Three read-only tools that surface customers and appointments from a
user's ServiceTitan tenant:

* ``st_search_customers`` -- substring search by name or phone.
* ``st_get_customer`` -- full record by numeric id.
* ``st_list_appointments`` -- date-range filtered appointment list,
  defaulting to "today" when no range is given.

Tools are constructed by :func:`build_servicetitan_tools`, which the
``servicetitan`` data factory calls once the user has connected a
tenant. None of these tools mutate state, so they ship without an
``ApprovalPolicy`` and without a ``concurrency_group``.

This module also lives in the auto-discovery path:
``ensure_tool_modules_imported`` imports every ``*_tools`` module in
``backend.app.agent.tools`` at startup. The import is side-effect-free
here (no ``_register()`` call); registration of the data factory lives
in ``backend/app/integrations/servicetitan/factory.py`` where the
companion ``servicetitan_auth`` factory and ``auth_check`` are wired.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.servicetitan.params import (
    StGetCustomerParams,
    StListAppointmentsParams,
    StSearchCustomersParams,
)
from backend.app.integrations.servicetitan.service import (
    ServiceTitanError,
    ServiceTitanNotConnectedError,
    ServiceTitanService,
)

logger = logging.getLogger(__name__)


# How many records to display in chat output. ServiceTitan paginates
# at 50 per page by default; the agent's chat window stays usable up
# to roughly this many lines before the model starts truncating.
_MAX_RESULTS_DEFAULT = 5
_MAX_RESULTS_HARD_CAP = 25

# Threshold for "looks like a phone number". Anything with more than
# this many digit characters in the query string is treated as a phone
# search instead of a name search.
_PHONE_DIGITS_THRESHOLD = 4


def _is_phone_query(query: str) -> bool:
    """True when the query is mostly digits and should hit the phone filter."""
    digits = sum(1 for ch in query if ch.isdigit())
    return digits >= _PHONE_DIGITS_THRESHOLD


def _service_error(label: str, exc: Exception) -> ToolResult:
    """Convert an exception from the service layer into a ToolResult."""
    if isinstance(exc, ServiceTitanNotConnectedError):
        return ToolResult(
            content=f"ServiceTitan is not connected (while {label}).",
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=(
                "Ask the user to run connect_servicetitan with their Tenant"
                " ID, Client ID, and Client Secret."
            ),
        )
    if isinstance(exc, ServiceTitanError):
        return ToolResult(
            content=f"ServiceTitan error while {label}: {exc}",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )
    logger.exception("Unexpected ServiceTitan failure %s", label)
    return ToolResult(
        content=f"Unexpected error while {label}: {exc}",
        is_error=True,
        error_kind=ToolErrorKind.INTERNAL,
    )


def _format_address(addr: dict[str, Any] | None) -> str:
    """Render an address sub-object into a single line, skipping blanks."""
    if not isinstance(addr, dict):
        return ""
    parts: list[str] = []
    street = addr.get("street")
    unit = addr.get("unit")
    if street:
        parts.append(f"{street} {unit}" if unit else str(street))
    city_state_zip: list[str] = []
    if addr.get("city"):
        city_state_zip.append(str(addr["city"]))
    if addr.get("state"):
        city_state_zip.append(str(addr["state"]))
    if addr.get("zip"):
        city_state_zip.append(str(addr["zip"]))
    if city_state_zip:
        parts.append(", ".join(city_state_zip))
    return " | ".join(parts)


def _format_contacts(contacts: list[dict[str, Any]] | None) -> str:
    """Render a customer's contact list as ``Type: value`` pairs."""
    if not isinstance(contacts, list) or not contacts:
        return ""
    rendered: list[str] = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type") or "Contact"
        value = c.get("value")
        if value:
            rendered.append(f"{ctype}: {value}")
    return ", ".join(rendered)


def _customer_summary_line(customer: dict[str, Any]) -> str:
    """One-line summary used in the search results list."""
    cid = customer.get("id", "?")
    name = customer.get("name") or "(no name)"
    ctype = customer.get("type") or ""
    address = _format_address(customer.get("address"))
    contacts = _format_contacts(customer.get("contacts"))
    pieces = [f"#{cid}", str(name)]
    if ctype:
        pieces.append(f"[{ctype}]")
    if address:
        pieces.append(address)
    if contacts:
        pieces.append(contacts)
    return " | ".join(pieces)


def _format_customer_detail(customer: dict[str, Any]) -> str:
    """Multi-line full record used by ``st_get_customer``."""
    cid = customer.get("id", "?")
    name = customer.get("name") or "(no name)"
    lines = [f"Customer #{cid}: {name}"]
    ctype = customer.get("type")
    if ctype:
        lines.append(f"  Type: {ctype}")
    address = _format_address(customer.get("address"))
    if address:
        lines.append(f"  Address: {address}")
    contacts = _format_contacts(customer.get("contacts"))
    if contacts:
        lines.append(f"  Contacts: {contacts}")
    if customer.get("balance") is not None:
        try:
            balance = float(customer["balance"])
            lines.append(f"  Balance: ${balance:,.2f}")
        except (TypeError, ValueError):
            lines.append(f"  Balance: {customer['balance']}")
    flags: list[str] = []
    if customer.get("active") is False:
        flags.append("inactive")
    if customer.get("doNotMail"):
        flags.append("do not mail")
    if customer.get("doNotService"):
        flags.append("do not service")
    if flags:
        lines.append(f"  Flags: {', '.join(flags)}")
    return "\n".join(lines)


def _format_appointment_line(appt: dict[str, Any]) -> str:
    """One-line summary used in the appointment list."""
    aid = appt.get("id", "?")
    job_id = appt.get("jobId", "?")
    start = appt.get("start") or "?"
    end = appt.get("end") or "?"
    status = appt.get("status") or "?"
    tech_ids = appt.get("technicianIds") or []
    pieces = [f"#{aid}", f"job={job_id}", f"start={start}", f"end={end}", f"[{status}]"]
    if isinstance(tech_ids, list) and tech_ids:
        pieces.append("techs=" + ",".join(str(t) for t in tech_ids))
    return " | ".join(pieces)


def _data_from_envelope(payload: Any) -> list[dict[str, Any]]:
    """Pull the ``data`` list out of ServiceTitan's pagination envelope."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _default_appointment_range() -> tuple[str, str]:
    """Compute the default "today (UTC)" window for st_list_appointments."""
    today = datetime.now(UTC).date()
    start = datetime.combine(today, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return _iso_z(start), _iso_z(end)


def _iso_z(dt: datetime) -> str:
    """Render a UTC datetime in the ``...Z`` form ServiceTitan expects."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_servicetitan_tools(service: ServiceTitanService) -> list[Tool]:
    """Build the ServiceTitan read tools bound to one user's service.

    The service argument carries the tenant id and authenticated HTTP
    client. Tools are closures so the agent invokes them with just
    their pydantic params, mirroring the AppFolio builder pattern.
    """

    async def st_search_customers(
        query: str,
        limit: int = _MAX_RESULTS_DEFAULT,
    ) -> ToolResult:
        """Search ServiceTitan customers by name or phone substring."""
        trimmed = query.strip()
        if not trimmed:
            return ToolResult(
                content="Search query is empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Pass a name fragment or partial phone number.",
            )
        # Clamp the limit so a hallucinated value cannot blow up the
        # chat response. Pydantic already validates the range via
        # StSearchCustomersParams; this is belt-and-suspenders for any
        # direct caller that bypasses the params model.
        capped_limit = max(1, min(int(limit), _MAX_RESULTS_HARD_CAP))

        params: dict[str, Any] = {}
        if _is_phone_query(trimmed):
            params["phone"] = trimmed
        else:
            params["name"] = trimmed

        path = f"/crm/v2/tenant/{service.tenant_id}/customers"
        try:
            payload = await service.get(path, params=params)
        except Exception as exc:
            return _service_error("searching customers", exc)

        records = _data_from_envelope(payload)
        if not records:
            return ToolResult(content=f"No customers matched {trimmed!r}.")

        truncated = records[:capped_limit]
        lines = [f"{len(records)} customer(s) matched {trimmed!r}:"]
        lines.extend(_customer_summary_line(c) for c in truncated)
        if len(records) > capped_limit:
            lines.append(
                f"... and {len(records) - capped_limit} more (raise limit or narrow the query)."
            )
        return ToolResult(content="\n".join(lines))

    async def st_get_customer(customer_id: int) -> ToolResult:
        """Fetch one customer record by its numeric id."""
        path = f"/crm/v2/tenant/{service.tenant_id}/customers/{customer_id}"
        try:
            payload = await service.get(path)
        except ServiceTitanError as exc:
            # The service layer raises ServiceTitanError on any 4xx/5xx.
            # The most common 4xx here is the 404 the fake and real
            # APIs return for an unknown id. Surface that as NOT_FOUND
            # so the agent can offer a follow-up search instead of
            # treating it as a transient SERVICE failure.
            text = str(exc)
            if "HTTP 404" in text:
                return ToolResult(
                    content=f"No customer with id {customer_id} in ServiceTitan.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                    hint="Run st_search_customers to find the correct id.",
                )
            return _service_error("fetching customer", exc)
        except Exception as exc:
            return _service_error("fetching customer", exc)

        if not isinstance(payload, dict) or not payload:
            return ToolResult(
                content=f"ServiceTitan returned no record for customer {customer_id}.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        return ToolResult(content=_format_customer_detail(payload))

    async def st_list_appointments(
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
    ) -> ToolResult:
        """List ServiceTitan appointments in a date range, optionally by status."""
        if not from_date and not to_date:
            from_date, to_date = _default_appointment_range()

        params: dict[str, Any] = {}
        if from_date:
            params["startsOnOrAfter"] = from_date
        if to_date:
            params["startsBefore"] = to_date
        if status:
            params["status"] = status

        # Sorting by start ascending so today's dispatch view reads in
        # the order a coordinator expects.
        params.setdefault("sort", "+Start")

        path = f"/jpm/v2/tenant/{service.tenant_id}/appointments"
        try:
            payload = await service.get(path, params=params)
        except Exception as exc:
            return _service_error("listing appointments", exc)

        records = _data_from_envelope(payload)
        if not records:
            window = f"{from_date or 'any'} -> {to_date or 'any'}"
            status_clause = f" with status {status}" if status else ""
            return ToolResult(
                content=f"No ServiceTitan appointments found for {window}{status_clause}."
            )

        lines = [f"Found {len(records)} appointment(s):"]
        lines.extend(_format_appointment_line(a) for a in records)
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.SERVICETITAN_SEARCH_CUSTOMERS,
            description=(
                "Search ServiceTitan customers by name or phone substring."
                " Returns a compact list of matches with id, name, type,"
                " address, and contacts. Use this before st_get_customer"
                " when only a name or phone fragment is known."
            ),
            function=st_search_customers,
            params_model=StSearchCustomersParams,
            usage_hint=(
                "Pass a name fragment (e.g. 'Acme', 'Jane Doe') or a"
                " partial phone number (e.g. '5550101'). Tool detects"
                " numeric queries and routes them to the phone filter."
            ),
        ),
        Tool(
            name=ToolName.SERVICETITAN_GET_CUSTOMER,
            description=(
                "Fetch the full ServiceTitan customer record by numeric"
                " id. Returns name, type, address, contacts, balance,"
                " and flags (inactive, do-not-mail, do-not-service)."
            ),
            function=st_get_customer,
            params_model=StGetCustomerParams,
            usage_hint=(
                "Use after st_search_customers has yielded a confirmed"
                " customer id. Returns NOT_FOUND when the id does not"
                " exist in the tenant."
            ),
        ),
        Tool(
            name=ToolName.SERVICETITAN_LIST_APPOINTMENTS,
            description=(
                "List ServiceTitan appointments in a date range. Defaults"
                " to today (UTC) when no dates are given. Optionally"
                " filter by appointment status. Returns id, jobId,"
                " start/end, status, and assigned technician ids."
            ),
            function=st_list_appointments,
            params_model=StListAppointmentsParams,
            usage_hint=(
                "Call with no arguments for today's dispatch view. Pass"
                " from_date / to_date (ISO 8601) to widen or narrow the"
                " window. Pass status to filter (Scheduled, Dispatched,"
                " Working, Done, Hold)."
            ),
        ),
    ]
