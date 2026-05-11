"""In-process fake of the ServiceTitan REST API.

ServiceTitan does not currently provide a sandbox tenant we can call.
Until that lands, every layer of the integration (auth, read tools,
write tools, agent flows, CI) needs a deterministic backend that
behaves like the real API at the wire level. This module provides
that backend.

The fake is exposed two ways:

* :class:`ServiceTitanFakeBackend` holds the seed dataset and routes
  raw method/path requests to handler functions. It carries no httpx
  dependency in its public surface, so tools can call it directly in
  unit tests when convenient.

* :func:`build_fake_transport` wraps a backend instance in an
  :class:`httpx.MockTransport` so a real ``httpx.AsyncClient`` can be
  pointed at it. This is the path production code will use when the
  ``servicetitan_use_fake`` setting is true (wired in the auth
  scaffold issue): the service layer constructs an httpx client with
  the fake transport instead of a real network transport, and every
  call site stays identical to what it will be against the live API.

The seed dataset is deliberately small (10 customers, 30 jobs, 15
appointments) but representative: a mix of Residential and Commercial
customers, HVAC / Plumbing / Electrical work, jobs in every common
status (Scheduled, Dispatched, InProgress, Completed, Hold, Canceled),
and appointments scattered around a fixed reference date ("today") so
date-range filters have something to find. Names and contact details
are obviously synthetic per the project-wide PII rules: "Acme
Plumbing", "Jane Doe", phone numbers in the 555 range, "123 Main St".

Endpoint shapes follow the public ServiceTitan OpenAPI spec
(developer.servicetitan.io) for the endpoints the MVP needs:

* ``POST /connect/token`` (15-minute Bearer)
* ``GET /crm/v2/tenant/{tenant}/customers`` (list with search filters)
* ``GET /crm/v2/tenant/{tenant}/customers/{id}`` (single record, 404 on miss)
* ``GET /crm/v2/tenant/{tenant}/customers/{id}/contacts``
* ``GET /jpm/v2/tenant/{tenant}/jobs`` (list with filters)
* ``GET /jpm/v2/tenant/{tenant}/jobs/{id}``
* ``GET /jpm/v2/tenant/{tenant}/jobs/{id}/notes``
* ``POST /jpm/v2/tenant/{tenant}/jobs/{id}/notes``
* ``GET /jpm/v2/tenant/{tenant}/appointments`` (list, date-range filtered)
* ``GET /jpm/v2/tenant/{tenant}/appointments/{id}``

Auth + error semantics that real ServiceTitan also surfaces:

* The token endpoint expects ``grant_type=client_credentials`` form
  data and returns ``{access_token, token_type, expires_in}``.
* Resource endpoints require both an ``Authorization: Bearer <token>``
  header and an ``ST-App-Key`` header. Missing or invalid token
  returns 401; missing app key returns 401 with a distinct payload.
* Expired tokens return 401 so refresh code paths can be exercised.
* Unknown customer / job / appointment IDs return 404 with the
  ServiceTitan-shaped error envelope.
* A rate-limit override (``ServiceTitanFakeBackend.force_rate_limit_for(n)``)
  makes the next ``n`` resource calls return 429 with a ``Retry-After``
  header, so retry/backoff code paths can be tested.

The fake is read-mostly: the only mutating endpoint right now is
``POST /jobs/{id}/notes``, which appends to the in-memory note list
and bumps the job's ``modifiedOn`` so downstream "show me what changed"
flows have a real signal to react to.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default tenant ID used by the seed dataset. Callers can route requests
# to any tenant ID; the backend only inspects the path segment, but
# tools and tests will most commonly use this value.
DEFAULT_TENANT_ID = 1234567

# The Bearer token returned by ``POST /connect/token``. Constant so tests
# can hardcode it; production swaps for a real ServiceTitan token.
FAKE_ACCESS_TOKEN = "fake-st-access-token-not-a-real-jwt"

# 15-minute lifetime, mirroring the real ServiceTitan token TTL.
ACCESS_TOKEN_TTL_SECONDS = 15 * 60

# Reference "today" the seed appointments cluster around. Using a fixed
# date keeps tests deterministic; the helper ``relative_to_today`` lets
# callers slide the whole calendar if they want to test date-range
# logic against ``datetime.now()`` semantics.
SEED_TODAY = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seed dataset
# ---------------------------------------------------------------------------

# Customer types in ServiceTitan are typically "Residential" or
# "Commercial"; the API exposes the value as a free-form string but
# the live tenant constrains it via enum. The fake mirrors that shape.
CUSTOMER_TYPE_RESIDENTIAL = "Residential"
CUSTOMER_TYPE_COMMERCIAL = "Commercial"

# Contact types the real API returns on customer contact records.
CONTACT_TYPE_PHONE = "Phone"
CONTACT_TYPE_MOBILE = "MobilePhone"
CONTACT_TYPE_EMAIL = "Email"

# Job-status enum values observed on the live API.
JOB_STATUS_SCHEDULED = "Scheduled"
JOB_STATUS_DISPATCHED = "Dispatched"
JOB_STATUS_IN_PROGRESS = "InProgress"
JOB_STATUS_COMPLETED = "Completed"
JOB_STATUS_HOLD = "Hold"
JOB_STATUS_CANCELED = "Canceled"

# Appointment-status enum values.
APPT_STATUS_SCHEDULED = "Scheduled"
APPT_STATUS_DISPATCHED = "Dispatched"
APPT_STATUS_WORKING = "Working"
APPT_STATUS_DONE = "Done"
APPT_STATUS_HOLD = "Hold"


def _iso(dt: datetime) -> str:
    """Format a UTC datetime the way the ServiceTitan API serializes them."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed_customers() -> list[dict[str, Any]]:
    """Build the seed customer records.

    Ten customers split 7/3 residential/commercial, each with a
    primary address and one or two contacts (phone + optionally email
    or mobile). IDs start at 1001 to make the integer "looks like a
    real ServiceTitan ID" without leaking real customer numbers.
    """
    now_iso = _iso(SEED_TODAY - timedelta(days=365))
    mod_iso = _iso(SEED_TODAY - timedelta(days=7))

    raw: list[dict[str, Any]] = [
        {
            "id": 1001,
            "name": "Jane Doe",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "123 Main St",
                "city": "Anytown",
                "state": "PA",
                "zip": "19103",
                "country": "USA",
            },
            "contacts": [
                {"id": 5001, "type": CONTACT_TYPE_PHONE, "value": "+15555550101"},
                {"id": 5002, "type": CONTACT_TYPE_EMAIL, "value": "jane.doe@example.com"},
            ],
        },
        {
            "id": 1002,
            "name": "John Roe",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "456 Oak Ave",
                "city": "Anytown",
                "state": "PA",
                "zip": "19104",
                "country": "USA",
            },
            "contacts": [
                {"id": 5003, "type": CONTACT_TYPE_MOBILE, "value": "+15555550102"},
            ],
        },
        {
            "id": 1003,
            "name": "Acme Plumbing",
            "type": CUSTOMER_TYPE_COMMERCIAL,
            "address": {
                "street": "789 Industry Park",
                "unit": "Suite 200",
                "city": "Commerce City",
                "state": "PA",
                "zip": "19105",
                "country": "USA",
            },
            "contacts": [
                {"id": 5004, "type": CONTACT_TYPE_PHONE, "value": "+15555550103"},
                {"id": 5005, "type": CONTACT_TYPE_EMAIL, "value": "ops@acme-plumbing.example.com"},
            ],
        },
        {
            "id": 1004,
            "name": "Alice Smith",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "12 Elm St",
                "city": "Anytown",
                "state": "PA",
                "zip": "19106",
                "country": "USA",
            },
            "contacts": [
                {"id": 5006, "type": CONTACT_TYPE_PHONE, "value": "+15555550104"},
                {"id": 5007, "type": CONTACT_TYPE_EMAIL, "value": "alice.smith@example.com"},
            ],
        },
        {
            "id": 1005,
            "name": "Bob Johnson",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "34 Pine St",
                "city": "Anytown",
                "state": "PA",
                "zip": "19107",
                "country": "USA",
            },
            "contacts": [
                {"id": 5008, "type": CONTACT_TYPE_MOBILE, "value": "+15555550105"},
            ],
        },
        {
            "id": 1006,
            "name": "Carol Williams",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "56 Cedar Ln",
                "city": "Other Town",
                "state": "NJ",
                "zip": "08001",
                "country": "USA",
            },
            "contacts": [
                {"id": 5009, "type": CONTACT_TYPE_PHONE, "value": "+15555550106"},
                {"id": 5010, "type": CONTACT_TYPE_EMAIL, "value": "carol.williams@example.com"},
            ],
        },
        {
            "id": 1007,
            "name": "Globex Property Management",
            "type": CUSTOMER_TYPE_COMMERCIAL,
            "address": {
                "street": "100 Corporate Blvd",
                "unit": "Floor 5",
                "city": "Big City",
                "state": "NJ",
                "zip": "08002",
                "country": "USA",
            },
            "contacts": [
                {"id": 5011, "type": CONTACT_TYPE_PHONE, "value": "+15555550107"},
                {
                    "id": 5012,
                    "type": CONTACT_TYPE_EMAIL,
                    "value": "facilities@globex-pm.example.com",
                },
            ],
        },
        {
            "id": 1008,
            "name": "David Brown",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "78 Maple Dr",
                "city": "Anytown",
                "state": "PA",
                "zip": "19108",
                "country": "USA",
            },
            "contacts": [
                {"id": 5013, "type": CONTACT_TYPE_PHONE, "value": "+15555550108"},
            ],
        },
        {
            "id": 1009,
            "name": "Eve Davis",
            "type": CUSTOMER_TYPE_RESIDENTIAL,
            "address": {
                "street": "90 Birch Ave",
                "city": "Anytown",
                "state": "PA",
                "zip": "19109",
                "country": "USA",
            },
            "contacts": [
                {"id": 5014, "type": CONTACT_TYPE_MOBILE, "value": "+15555550109"},
                {"id": 5015, "type": CONTACT_TYPE_EMAIL, "value": "eve.davis@example.com"},
            ],
        },
        {
            "id": 1010,
            "name": "Initech Holdings",
            "type": CUSTOMER_TYPE_COMMERCIAL,
            "address": {
                "street": "200 Tech Center Way",
                "city": "Big City",
                "state": "NJ",
                "zip": "08003",
                "country": "USA",
            },
            "contacts": [
                {"id": 5016, "type": CONTACT_TYPE_PHONE, "value": "+15555550110"},
                {"id": 5017, "type": CONTACT_TYPE_EMAIL, "value": "ap@initech.example.com"},
            ],
        },
    ]

    # Each customer record also exposes a location reference (the real
    # API has separate locations endpoints; the fake collapses each
    # customer's primary address into one synthetic location whose ID
    # equals the customer ID for now, since the MVP tools do not
    # exercise multi-location customers yet).
    for c in raw:
        c.setdefault("active", True)
        c.setdefault("doNotMail", False)
        c.setdefault("doNotService", False)
        c.setdefault("balance", 0.0)
        c.setdefault("createdOn", now_iso)
        c.setdefault("modifiedOn", mod_iso)
        c.setdefault("createdById", 1)
        c.setdefault("customFields", [])
    return raw


def _seed_jobs() -> list[dict[str, Any]]:
    """Build the seed job records.

    Thirty jobs distributed across the ten customers with a mix of
    HVAC, plumbing, and electrical work and every common status. The
    job ``summary`` field carries a short human description that
    tools can surface verbatim in chat output.
    """
    created_iso = _iso(SEED_TODAY - timedelta(days=30))
    mod_iso = _iso(SEED_TODAY - timedelta(days=1))

    # (customer_id, job_type, status, summary, business_unit_id)
    plan: list[tuple[int, str, str, str, int]] = [
        # HVAC mix
        (1001, "HVAC", JOB_STATUS_COMPLETED, "AC tune-up, replaced capacitor", 10),
        (1001, "HVAC", JOB_STATUS_SCHEDULED, "Annual furnace maintenance", 10),
        (1002, "HVAC", JOB_STATUS_DISPATCHED, "No cooling diagnostic", 10),
        (1002, "HVAC", JOB_STATUS_COMPLETED, "Replaced thermostat", 10),
        (1004, "HVAC", JOB_STATUS_IN_PROGRESS, "Heat pump installation, day 2 of 3", 10),
        (1006, "HVAC", JOB_STATUS_HOLD, "Awaiting condenser part backorder", 10),
        (1008, "HVAC", JOB_STATUS_COMPLETED, "Duct cleaning", 10),
        (1009, "HVAC", JOB_STATUS_SCHEDULED, "AC seasonal start-up", 10),
        # Plumbing mix
        (1001, "Plumbing", JOB_STATUS_COMPLETED, "Replaced kitchen disposal", 20),
        (1003, "Plumbing", JOB_STATUS_IN_PROGRESS, "Commercial restroom repipe", 20),
        (1003, "Plumbing", JOB_STATUS_COMPLETED, "Backflow preventer test", 20),
        (1004, "Plumbing", JOB_STATUS_SCHEDULED, "Water heater replacement quote", 20),
        (1005, "Plumbing", JOB_STATUS_DISPATCHED, "Leaking shower valve", 20),
        (1005, "Plumbing", JOB_STATUS_CANCELED, "Customer rescheduled with neighbor", 20),
        (1007, "Plumbing", JOB_STATUS_SCHEDULED, "Quarterly grease trap service", 20),
        (1008, "Plumbing", JOB_STATUS_COMPLETED, "Toilet flange repair", 20),
        (1010, "Plumbing", JOB_STATUS_IN_PROGRESS, "Office building water main shutoff valve", 20),
        # Electrical mix
        (1002, "Electrical", JOB_STATUS_COMPLETED, "Replaced GFCI outlets in kitchen", 30),
        (1004, "Electrical", JOB_STATUS_COMPLETED, "Ceiling fan installation", 30),
        (1006, "Electrical", JOB_STATUS_SCHEDULED, "Whole-home surge protector add-on", 30),
        (1006, "Electrical", JOB_STATUS_DISPATCHED, "Breaker tripping intermittently", 30),
        (1007, "Electrical", JOB_STATUS_IN_PROGRESS, "Parking lot pole-light retrofit", 30),
        (1009, "Electrical", JOB_STATUS_HOLD, "Permit pending for sub-panel upgrade", 30),
        (1010, "Electrical", JOB_STATUS_COMPLETED, "EV charger 240V install", 30),
        # Mixed leftovers to round out edge statuses
        (1001, "Electrical", JOB_STATUS_CANCELED, "Outlet add-on, customer canceled", 30),
        (1003, "HVAC", JOB_STATUS_SCHEDULED, "Rooftop unit quarterly PM", 10),
        (1005, "Electrical", JOB_STATUS_SCHEDULED, "Smoke detector replacements", 30),
        (1007, "HVAC", JOB_STATUS_COMPLETED, "Walk-in cooler thermostat swap", 10),
        (1008, "Electrical", JOB_STATUS_SCHEDULED, "Replace porch light fixture", 30),
        (1010, "HVAC", JOB_STATUS_IN_PROGRESS, "Server room mini-split commissioning", 10),
    ]

    jobs: list[dict[str, Any]] = []
    for i, (cust_id, job_type, status, summary, bu_id) in enumerate(plan, start=1):
        job_id = 2000 + i
        completed_on = (
            _iso(SEED_TODAY - timedelta(days=5)) if status == JOB_STATUS_COMPLETED else None
        )
        jobs.append(
            {
                "id": job_id,
                "jobNumber": f"J-{job_id}",
                "customerId": cust_id,
                "locationId": cust_id,  # single-location-per-customer simplification
                "jobStatus": status,
                "completedOn": completed_on,
                "businessUnitId": bu_id,
                "jobTypeId": {"HVAC": 1, "Plumbing": 2, "Electrical": 3}[job_type],
                "priority": "Normal",
                "campaignId": 100,
                "summary": summary,
                "customFields": [],
                "appointmentCount": 0,  # patched below once appointments are seeded
                "firstAppointmentId": None,
                "lastAppointmentId": None,
                # These six fields are nullable in the live ``CrmV2JobResponse``
                # but are always serialized. Surfacing them as ``None`` keeps
                # the wire shape complete so tools that probe for their presence
                # behave the same against the fake and the real API.
                "recallForId": None,
                "warrantyId": None,
                "jobGeneratedLeadSource": None,
                "leadCallId": None,
                "bookingId": None,
                "soldById": None,
                "noCharge": False,
                "notificationsEnabled": True,
                "createdOn": created_iso,
                "createdById": 1,
                "modifiedOn": mod_iso,
                "tagTypeIds": [],
                "externalData": [],
            }
        )
    return jobs


def _seed_appointments(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the seed appointment records.

    Fifteen appointments spread across "today" plus or minus a few
    days. Each appointment references a real job. The ``status``
    reflects the job's lifecycle state at the seed point. Appointments
    are anchored to ``SEED_TODAY`` so date-range filters like "today"
    and "this week" exercise real filtering logic.
    """
    # Pick a stable subset of jobs to schedule appointments against.
    # Includes a job in every common status so today's dispatch view
    # has variety.
    targets = [
        (jobs[0], -2, 9),  # completed HVAC, two days ago
        (jobs[2], 0, 8),  # dispatched HVAC, today 8am
        (jobs[4], 0, 11),  # in-progress HVAC install, midday
        (jobs[4], 1, 8),  # follow-up day
        (jobs[5], -5, 14),  # hold, before going on hold
        (jobs[8], -1, 10),  # completed plumbing, yesterday
        (jobs[9], 0, 13),  # in-progress plumbing midday
        (jobs[11], 1, 9),  # scheduled plumbing tomorrow
        (jobs[12], 0, 7),  # dispatched plumbing early today
        (jobs[14], 2, 9),  # scheduled plumbing day after tomorrow
        (jobs[17], -3, 10),  # completed electrical
        (jobs[20], 0, 15),  # dispatched electrical late today
        (jobs[21], 0, 9),  # in-progress electrical today
        (jobs[23], -7, 9),  # completed electrical last week
        (jobs[26], 3, 10),  # scheduled electrical later this week
    ]

    # Synthetic technician IDs assigned in round-robin so dispatch-style
    # tools can group appointments by tech. Three techs are enough to
    # exercise the "who's on this job" surface area without bloating the
    # seed.
    tech_pool = [101, 102, 103]

    appts: list[dict[str, Any]] = []
    for appt_seq, (job, day_offset, hour) in enumerate(targets, start=1):
        appt_id = 3000 + appt_seq
        start = SEED_TODAY.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(
            days=day_offset
        )
        end = start + timedelta(hours=2)
        arr_start = start - timedelta(minutes=30)
        arr_end = start + timedelta(minutes=30)

        appt_status = {
            JOB_STATUS_SCHEDULED: APPT_STATUS_SCHEDULED,
            JOB_STATUS_DISPATCHED: APPT_STATUS_DISPATCHED,
            JOB_STATUS_IN_PROGRESS: APPT_STATUS_WORKING,
            JOB_STATUS_COMPLETED: APPT_STATUS_DONE,
            JOB_STATUS_HOLD: APPT_STATUS_HOLD,
            JOB_STATUS_CANCELED: APPT_STATUS_HOLD,
        }[job["jobStatus"]]

        # Real ``JpmV2AppointmentResponse`` ships ``technicianIds`` as a
        # plural list because one appointment can be co-dispatched. Most
        # appointments here get one tech; appointments on in-progress
        # multi-day work get two so the second-tech case is exercised.
        assigned: list[int] = [tech_pool[appt_seq % len(tech_pool)]]
        if job["jobStatus"] == JOB_STATUS_IN_PROGRESS and appt_seq % 2 == 0:
            assigned.append(tech_pool[(appt_seq + 1) % len(tech_pool)])

        appts.append(
            {
                "id": appt_id,
                "jobId": job["id"],
                "appointmentNumber": f"A-{appt_id}",
                "start": _iso(start),
                "end": _iso(end),
                "arrivalWindowStart": _iso(arr_start),
                "arrivalWindowEnd": _iso(arr_end),
                "status": appt_status,
                "specialInstructions": None,
                "technicianIds": assigned,
                "createdOn": _iso(start - timedelta(days=7)),
                "modifiedOn": _iso(start - timedelta(days=1)),
            }
        )

        # Patch the job's appointment counters so the job record
        # advertises the appointments the way the real API does.
        job["appointmentCount"] = (job.get("appointmentCount") or 0) + 1
        if job["firstAppointmentId"] is None:
            job["firstAppointmentId"] = appt_id
        job["lastAppointmentId"] = appt_id

    return appts


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@dataclass
class _IssuedToken:
    """An access token the fake has minted via ``POST /connect/token``."""

    value: str
    issued_at: datetime
    expires_at: datetime
    expired: bool = False

    def is_valid(self, now: datetime) -> bool:
        if self.expired:
            return False
        return now < self.expires_at


@dataclass
class ServiceTitanFakeBackend:
    """In-memory ServiceTitan API double.

    The dataclass fields constitute the entire state: seed records,
    issued tokens, and override flags for forced rate-limit / expired
    token responses. Tests construct one per case and pass it to
    :func:`build_fake_transport`.

    The state is intentionally not thread-safe. The integration calls
    the fake serially per request from a single asyncio task, which
    matches the locality the real API guarantees.
    """

    # The tenant the seed data lives under. Requests for other tenants
    # return empty lists / 404s, mirroring real ServiceTitan multi-
    # tenant isolation.
    tenant_id: int = DEFAULT_TENANT_ID
    app_key: str = "fake-st-app-key"
    customers: list[dict[str, Any]] = field(default_factory=_seed_customers)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    appointments: list[dict[str, Any]] = field(default_factory=list)
    tokens: list[_IssuedToken] = field(default_factory=list)
    # Number of resource calls to fail with HTTP 429 before resuming
    # normal behavior. Decremented each time a 429 is served.
    rate_limit_calls_remaining: int = 0

    def __post_init__(self) -> None:
        if not self.jobs:
            self.jobs = _seed_jobs()
        if not self.appointments:
            self.appointments = _seed_appointments(self.jobs)

    # -- public helpers used by tests ------------------------------------

    def expire_all_tokens(self) -> None:
        """Mark every previously-issued token as expired.

        Useful for testing the refresh path: after this call the next
        resource request with the old Bearer returns 401.
        """
        for tok in self.tokens:
            tok.expired = True

    def force_rate_limit_for(self, n: int) -> None:
        """Make the next ``n`` resource calls return HTTP 429.

        ``/connect/token`` is never rate-limited; only resource paths
        are affected, matching the real API.
        """
        self.rate_limit_calls_remaining = max(self.rate_limit_calls_remaining, n)

    def issue_token(self, *, now: datetime | None = None) -> _IssuedToken:
        """Mint a new access token and record it."""
        moment = now or datetime.now(UTC)
        tok = _IssuedToken(
            value=FAKE_ACCESS_TOKEN,
            issued_at=moment,
            expires_at=moment + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
        )
        self.tokens.append(tok)
        return tok

    # -- request entry point --------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Dispatch an httpx request to the right handler.

        This is the function passed to :class:`httpx.MockTransport`.
        It branches first on path prefix (token vs resource), then on
        method + parameterized path within each namespace.
        """
        path = request.url.path
        method = request.method.upper()

        if path == "/connect/token":
            return self._handle_token(request)

        # Every resource path requires authentication.
        auth_err = self._authenticate(request)
        if auth_err is not None:
            return auth_err

        if self.rate_limit_calls_remaining > 0:
            self.rate_limit_calls_remaining -= 1
            return _json_response(
                429,
                {
                    "type": "https://api.servicetitan.io/errors/rate-limit",
                    "title": "Rate limit exceeded",
                    "status": 429,
                    "detail": "Too many requests; back off and retry.",
                },
                headers={"Retry-After": "1"},
            )

        # CRM customers namespace.
        crm_prefix = f"/crm/v2/tenant/{self.tenant_id}/customers"
        if path.startswith(crm_prefix):
            return self._handle_crm_customers(method, path[len(crm_prefix) :], request)

        # JPM jobs + appointments namespace.
        jpm_prefix = f"/jpm/v2/tenant/{self.tenant_id}"
        if path.startswith(jpm_prefix):
            return self._handle_jpm(method, path[len(jpm_prefix) :], request)

        # Any tenant that is not the seeded one: return empty list
        # for collection GETs, 404 for everything else. This mirrors
        # ServiceTitan's per-tenant scoping where the wrong tenant
        # simply has no data.
        if (
            ("/customers" in path or "/jobs" in path or "/appointments" in path)
            and method == "GET"
            and not path.rstrip("/").rsplit("/", 1)[-1].isdigit()
        ):
            return _json_response(200, _paginated([]))
        return _not_found(path)

    # -- token endpoint --------------------------------------------------

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        body = request.content.decode("utf-8") if request.content else ""
        form = {k: v[0] for k, v in parse_qs(body).items()}
        if form.get("grant_type") != "client_credentials":
            return _json_response(
                400,
                {"error": "unsupported_grant_type", "error_description": "Use client_credentials."},
            )
        if not form.get("client_id") or not form.get("client_secret"):
            return _json_response(
                400,
                {
                    "error": "invalid_client",
                    "error_description": "client_id and client_secret are required.",
                },
            )
        tok = self.issue_token()
        return _json_response(
            200,
            {
                "access_token": tok.value,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "scope": "",
            },
        )

    # -- auth check on resource requests --------------------------------

    def _authenticate(self, request: httpx.Request) -> httpx.Response | None:
        """Return None when the request is authenticated, else a 401 response."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return _json_response(
                401,
                {
                    "type": "https://api.servicetitan.io/errors/unauthenticated",
                    "title": "Unauthenticated",
                    "status": 401,
                    "detail": "Missing or malformed Authorization header.",
                },
            )
        bearer = auth_header.split(" ", 1)[1].strip()
        now = datetime.now(UTC)
        valid = any(t.value == bearer and t.is_valid(now) for t in self.tokens)
        # If no token has been issued yet but the caller is presenting
        # the well-known constant, accept it. This lets quick tests
        # skip the token-handshake call when they only care about
        # resource shapes.
        if not valid and bearer == FAKE_ACCESS_TOKEN and not self.tokens:
            self.issue_token()
            valid = True
        if not valid:
            return _json_response(
                401,
                {
                    "type": "https://api.servicetitan.io/errors/unauthenticated",
                    "title": "Unauthenticated",
                    "status": 401,
                    "detail": "Access token is invalid or expired.",
                },
            )
        if not request.headers.get("st-app-key"):
            return _json_response(
                401,
                {
                    "type": "https://api.servicetitan.io/errors/missing-app-key",
                    "title": "Missing app key",
                    "status": 401,
                    "detail": "Requests must include the ST-App-Key header.",
                },
            )
        return None

    # -- CRM customers ---------------------------------------------------

    def _handle_crm_customers(
        self, method: str, suffix: str, request: httpx.Request
    ) -> httpx.Response:
        """Dispatch ``/crm/v2/tenant/{tenant}/customers...`` requests.

        ``suffix`` is the portion of the path after the prefix, e.g.
        empty string for the collection root, ``/1001`` for a single
        customer, ``/1001/contacts`` for the contacts subresource.
        """
        if suffix in ("", "/"):
            if method == "GET":
                return self._list_customers(request)
            return _json_response(405, {"error": "method_not_allowed"})

        parts = suffix.lstrip("/").split("/")
        if not parts[0].isdigit():
            return _not_found(request.url.path)
        cust_id = int(parts[0])
        customer = next((c for c in self.customers if c["id"] == cust_id), None)

        if len(parts) == 1:
            if method != "GET":
                return _json_response(405, {"error": "method_not_allowed"})
            if customer is None:
                return _not_found(request.url.path)
            return _json_response(200, deepcopy(customer))

        if len(parts) == 2 and parts[1] == "contacts":
            if method != "GET":
                return _json_response(405, {"error": "method_not_allowed"})
            if customer is None:
                return _not_found(request.url.path)
            return _json_response(200, _paginated(deepcopy(customer.get("contacts", []))))

        return _not_found(request.url.path)

    def _list_customers(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        results = [deepcopy(c) for c in self.customers]

        name_q = params.get("name")
        if name_q:
            needle = name_q.lower()
            results = [c for c in results if needle in c["name"].lower()]

        phone_q = params.get("phone")
        if phone_q:
            normalized = _digits_only(phone_q)
            results = [c for c in results if _customer_matches_phone(c, normalized)]

        # Address-level filters. ``street``, ``unit``, ``city``, ``state``,
        # ``zip``, ``country`` all match the address subobject; the real API
        # does case-insensitive substring on the text fields and exact match
        # on the structured fields. The fake collapses to case-insensitive
        # substring for simplicity, which is a strict superset of exact
        # match for normal callers.
        for param_name, addr_key in (
            ("street", "street"),
            ("unit", "unit"),
            ("city", "city"),
            ("state", "state"),
            ("zip", "zip"),
            ("country", "country"),
        ):
            value = params.get(param_name)
            if not value:
                continue
            needle = value.lower()
            results = [
                c for c in results if needle in str(c["address"].get(addr_key) or "").lower()
            ]

        # ``latitude`` and ``longitude`` are geographic filters in the real
        # API and are typically paired with a radius. The fake does exact
        # match against the address coordinates; no seed customer has
        # coordinates set, so unmodified seed data returns an empty list
        # for these. Callers that want to exercise geo filtering should
        # seed their own backend with coordinate-bearing records.
        for param_name in ("latitude", "longitude"):
            value = params.get(param_name)
            if not value:
                continue
            try:
                target = float(value)
            except ValueError:
                continue
            results = [c for c in results if c["address"].get(param_name) == target]

        active_q = params.get("active")
        if active_q and active_q.lower() != "any":
            want_active = active_q.lower() == "true"
            results = [c for c in results if c["active"] == want_active]

        ids_q = params.get("ids")
        if ids_q:
            wanted = {int(x) for x in ids_q.split(",") if x.strip().isdigit()}
            results = [c for c in results if c["id"] in wanted]

        results = _apply_date_range_filters(results, params)
        return _json_response(200, _paginated(results, request))

    # -- JPM jobs + appointments ----------------------------------------

    def _handle_jpm(self, method: str, suffix: str, request: httpx.Request) -> httpx.Response:
        if suffix.startswith("/jobs"):
            return self._handle_jobs(method, suffix[len("/jobs") :], request)
        if suffix.startswith("/appointments"):
            return self._handle_appointments(method, suffix[len("/appointments") :], request)
        return _not_found(request.url.path)

    def _handle_jobs(self, method: str, suffix: str, request: httpx.Request) -> httpx.Response:
        if suffix in ("", "/"):
            if method == "GET":
                return self._list_jobs(request)
            return _json_response(405, {"error": "method_not_allowed"})

        parts = suffix.lstrip("/").split("/")
        if not parts[0].isdigit():
            return _not_found(request.url.path)
        job_id = int(parts[0])
        job = next((j for j in self.jobs if j["id"] == job_id), None)

        if len(parts) == 1:
            if method != "GET":
                return _json_response(405, {"error": "method_not_allowed"})
            if job is None:
                return _not_found(request.url.path)
            return _json_response(200, deepcopy(job))

        if len(parts) == 2 and parts[1] == "notes":
            if job is None:
                return _not_found(request.url.path)
            if method == "GET":
                return _json_response(200, _paginated(deepcopy(_job_notes(job))))
            if method == "POST":
                return self._add_job_note(job, request)
            return _json_response(405, {"error": "method_not_allowed"})

        return _not_found(request.url.path)

    def _list_jobs(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        results = [deepcopy(j) for j in self.jobs]

        cust_q = params.get("customerId")
        if cust_q and cust_q.isdigit():
            cust_id = int(cust_q)
            results = [j for j in results if j["customerId"] == cust_id]

        status_q = params.get("jobStatus")
        if status_q:
            wanted = {s.strip() for s in status_q.split(",") if s.strip()}
            results = [j for j in results if j["jobStatus"] in wanted]

        bu_q = params.get("businessUnitIds")
        if bu_q:
            wanted_ids = {int(x) for x in bu_q.split(",") if x.strip().isdigit()}
            results = [j for j in results if j["businessUnitId"] in wanted_ids]

        ids_q = params.get("ids")
        if ids_q:
            wanted_job_ids = {int(x) for x in ids_q.split(",") if x.strip().isdigit()}
            results = [j for j in results if j["id"] in wanted_job_ids]

        # ``completedOnOrAfter`` / ``completedBefore`` are JPM-specific
        # date-range filters in addition to the standard created/modified
        # pair that every list endpoint supports.
        completed_after = params.get("completedOnOrAfter")
        if completed_after:
            cutoff = _parse_iso(completed_after)
            results = [
                j
                for j in results
                if j.get("completedOn") and _parse_iso(j["completedOn"]) >= cutoff
            ]
        completed_before = params.get("completedBefore")
        if completed_before:
            cutoff = _parse_iso(completed_before)
            results = [
                j for j in results if j.get("completedOn") and _parse_iso(j["completedOn"]) < cutoff
            ]

        results = _apply_date_range_filters(results, params)
        return _json_response(200, _paginated(results, request))

    def _add_job_note(self, job: dict[str, Any], request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        except json.JSONDecodeError:
            return _json_response(
                400,
                {
                    "type": "https://api.servicetitan.io/errors/invalid-body",
                    "title": "Invalid body",
                    "status": 400,
                    "detail": "Request body is not valid JSON.",
                },
            )
        text = (payload.get("text") or "").strip()
        if not text:
            return _json_response(
                400,
                {
                    "type": "https://api.servicetitan.io/errors/validation",
                    "title": "Validation failed",
                    "status": 400,
                    "detail": "text is required.",
                },
            )
        now = datetime.now(UTC)
        note = {
            "text": text,
            "isPinned": bool(payload.get("pinToTop", False)),
            "createdById": 1,
            "createdOn": _iso(now),
            "modifiedOn": _iso(now),
        }
        notes = job.setdefault("_notes", [])
        notes.append(note)
        job["modifiedOn"] = _iso(now)
        return _json_response(200, deepcopy(note))

    def _handle_appointments(
        self, method: str, suffix: str, request: httpx.Request
    ) -> httpx.Response:
        if suffix in ("", "/"):
            if method == "GET":
                return self._list_appointments(request)
            return _json_response(405, {"error": "method_not_allowed"})
        parts = suffix.lstrip("/").split("/")
        if not parts[0].isdigit():
            return _not_found(request.url.path)
        appt_id = int(parts[0])
        appt = next((a for a in self.appointments if a["id"] == appt_id), None)
        if len(parts) == 1:
            if method != "GET":
                return _json_response(405, {"error": "method_not_allowed"})
            if appt is None:
                return _not_found(request.url.path)
            return _json_response(200, deepcopy(appt))
        return _not_found(request.url.path)

    def _list_appointments(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        results = [deepcopy(a) for a in self.appointments]

        starts_on_or_after = params.get("startsOnOrAfter")
        if starts_on_or_after:
            cutoff = _parse_iso(starts_on_or_after)
            results = [a for a in results if _parse_iso(a["start"]) >= cutoff]
        starts_before = params.get("startsBefore")
        if starts_before:
            cutoff = _parse_iso(starts_before)
            results = [a for a in results if _parse_iso(a["start"]) < cutoff]

        job_q = params.get("jobId")
        if job_q and job_q.isdigit():
            jid = int(job_q)
            results = [a for a in results if a["jobId"] == jid]

        status_q = params.get("status")
        if status_q:
            wanted = {s.strip() for s in status_q.split(",") if s.strip()}
            results = [a for a in results if a["status"] in wanted]

        results = _apply_date_range_filters(results, params)
        return _json_response(200, _paginated(results, request))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(
    status: int, body: dict[str, Any] | list[Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    payload = json.dumps(body).encode("utf-8")
    merged_headers = {"Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    return httpx.Response(status_code=status, content=payload, headers=merged_headers)


def _not_found(path: str) -> httpx.Response:
    return _json_response(
        404,
        {
            "type": "https://api.servicetitan.io/errors/not-found",
            "title": "Resource not found",
            "status": 404,
            "detail": f"No resource at {path}.",
        },
    )


def _paginated(data: list[dict[str, Any]], request: httpx.Request | None = None) -> dict[str, Any]:
    """Wrap a list of records in ServiceTitan's pagination envelope.

    Honors ``page`` and ``pageSize`` for slicing, ``sort=+/-Field`` for
    ordering (recognized fields: ``Id``, ``ModifiedOn``, ``CreatedOn``,
    ``Start`` for appointments), and ``includeTotal=false`` to suppress
    the total count (signaled by ``totalCount: -1``, which is the
    sentinel commonly used by .NET-paginated APIs when the count is
    intentionally not computed).
    """
    page = 1
    page_size = 50
    include_total = True
    if request is not None:
        params = request.url.params
        sort_q = params.get("sort")
        if sort_q:
            data = _apply_sort(data, sort_q)
        page_str = params.get("page")
        if page_str and page_str.isdigit():
            page = max(int(page_str), 1)
        ps_str = params.get("pageSize")
        if ps_str and ps_str.isdigit():
            page_size = max(int(ps_str), 1)
        include_total_q = params.get("includeTotal")
        if include_total_q is not None and include_total_q.lower() == "false":
            include_total = False
    start = (page - 1) * page_size
    end = start + page_size
    slice_ = data[start:end]
    return {
        "page": page,
        "pageSize": page_size,
        "hasMore": end < len(data),
        "totalCount": len(data) if include_total else -1,
        "data": slice_,
    }


# Map sort field names (PascalCase per ServiceTitan docs) to the
# camelCase record keys in the fake. Anything else is ignored.
_SORT_FIELD_KEYS: dict[str, str] = {
    "Id": "id",
    "ModifiedOn": "modifiedOn",
    "CreatedOn": "createdOn",
    "Start": "start",
}


def _apply_sort(data: list[dict[str, Any]], sort_q: str) -> list[dict[str, Any]]:
    """Apply a ``sort=+/-Field`` query parameter to a record list."""
    descending = sort_q.startswith("-")
    field_name = sort_q.lstrip("+-")
    key = _SORT_FIELD_KEYS.get(field_name)
    if key is None:
        return data
    return sorted(data, key=lambda r: r.get(key) or "", reverse=descending)


def _apply_date_range_filters(
    data: list[dict[str, Any]],
    params: httpx.QueryParams,
    *,
    created_field: str = "createdOn",
    modified_field: str = "modifiedOn",
) -> list[dict[str, Any]]:
    """Apply the standard ``createdBefore`` / ``createdOnOrAfter`` /
    ``modifiedBefore`` / ``modifiedOnOrAfter`` filters every list endpoint
    in the real API supports.
    """
    spec = (
        ("createdBefore", "<", created_field),
        ("createdOnOrAfter", ">=", created_field),
        ("modifiedBefore", "<", modified_field),
        ("modifiedOnOrAfter", ">=", modified_field),
    )
    for param_name, comparator, record_field in spec:
        value = params.get(param_name)
        if not value:
            continue
        cutoff = _parse_iso(value)
        if comparator == "<":
            data = [r for r in data if r.get(record_field) and _parse_iso(r[record_field]) < cutoff]
        else:
            data = [
                r for r in data if r.get(record_field) and _parse_iso(r[record_field]) >= cutoff
            ]
    return data


def _job_notes(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface a job's notes list.

    Notes are stored on the job under the private ``_notes`` key so
    the user-facing job payload doesn't expose them; the real API
    serves them through the dedicated ``/notes`` subresource.
    """
    return job.get("_notes", [])


def _digits_only(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _customer_matches_phone(customer: dict[str, Any], digits: str) -> bool:
    """True when any of the customer's contacts contain the given digits."""
    if not digits:
        return True
    for contact in customer.get("contacts", []):
        if contact.get("type") in (CONTACT_TYPE_PHONE, CONTACT_TYPE_MOBILE) and digits in (
            _digits_only(contact.get("value", ""))
        ):
            return True
    return False


def _parse_iso(value: str) -> datetime:
    """Parse a ServiceTitan-formatted ISO timestamp into a UTC datetime."""
    cleaned = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_fake_transport(
    backend: ServiceTitanFakeBackend | None = None,
) -> httpx.MockTransport:
    """Return an httpx transport that serves requests from ``backend``.

    Production code that wants to talk to the fake constructs its
    ``httpx.AsyncClient`` with ``transport=build_fake_transport()``;
    the rest of the call site is identical to what it will be against
    a real ServiceTitan endpoint. When ``backend`` is omitted, a new
    backend with the default seed dataset is created and routed to.
    """
    target = backend or ServiceTitanFakeBackend()

    def handler(request: httpx.Request) -> httpx.Response:
        return target.handle(request)

    return httpx.MockTransport(handler)


_DEFAULT_BACKEND: ServiceTitanFakeBackend | None = None


def get_default_fake_backend() -> ServiceTitanFakeBackend:
    """Process-wide default backend.

    Useful for the integration's ``service.py`` when ``settings.
    servicetitan_use_fake`` is true: every async client constructed
    by the service routes through the same backend instance, so
    state mutations (a posted job note) are visible across requests
    within the process.
    """
    global _DEFAULT_BACKEND
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = ServiceTitanFakeBackend()
    return _DEFAULT_BACKEND


def reset_default_fake_backend() -> None:
    """Drop the cached default backend (used by tests for isolation)."""
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = None


# A side-effect-free way for downstream tools to discover the available
# seed records without poking at private state: tests in the read-tools
# issue (#1300) iterate over the result to assert their tools surface a
# stable subset.
def iter_seed_customer_ids(backend: ServiceTitanFakeBackend | None = None) -> Iterable[int]:
    target = backend or get_default_fake_backend()
    return (c["id"] for c in target.customers)


def iter_seed_job_ids(backend: ServiceTitanFakeBackend | None = None) -> Iterable[int]:
    target = backend or get_default_fake_backend()
    return (j["id"] for j in target.jobs)


# Re-exported so callers can resolve the constant without importing the
# private name. Useful when read tools build a fixture for the auth
# scaffold in #1298.
FAKE_TOKEN_VALUE: str = FAKE_ACCESS_TOKEN
FAKE_TOKEN_TTL: int = ACCESS_TOKEN_TTL_SECONDS

# Sanity check kept at import time so a regression in the seed builders
# fails fast in the test session rather than confusing a downstream
# tool author. Cheap (≤45 dicts) so the cost is irrelevant.
_sanity_backend = ServiceTitanFakeBackend()
assert len(_sanity_backend.customers) == 10, "expected 10 seed customers"
assert len(_sanity_backend.jobs) == 30, "expected 30 seed jobs"
assert len(_sanity_backend.appointments) == 15, "expected 15 seed appointments"
del _sanity_backend


# Type alias re-exported for the convenience of any downstream consumer
# that wants to declare a handler that accepts either a request or a
# raw method/path pair. Defined here so the underscore-prefixed module
# isn't reached into directly elsewhere.
RequestHandler = Callable[[httpx.Request], httpx.Response]
