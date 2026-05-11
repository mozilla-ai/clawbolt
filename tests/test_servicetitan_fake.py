"""Tests for the in-process ServiceTitan fake backend.

These tests exercise the fake by driving an ``httpx.AsyncClient``
through ``build_fake_transport``, mirroring how production code will
talk to the fake when ``servicetitan_use_fake=true``. The assertions
focus on three things:

* The wire-level response shapes match what the public ServiceTitan
  OpenAPI spec promises for each endpoint, so any tool written against
  the fake will keep working when the real API is wired up.
* The auth flow round-trips: ``POST /connect/token`` mints a Bearer,
  the Bearer unlocks resource endpoints, expired or missing Bearers
  surface 401, and missing ``ST-App-Key`` surfaces 401.
* The filtering helpers (name / phone / status / date range) and the
  rate-limit override behave as the integration code expects.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.integrations.servicetitan import (
    ServiceTitanFakeBackend,
    build_fake_transport,
)
from backend.app.integrations.servicetitan._fake import (
    ACCESS_TOKEN_TTL_SECONDS,
    APPT_STATUS_DISPATCHED,
    APPT_STATUS_SCHEDULED,
    DEFAULT_TENANT_ID,
    FAKE_ACCESS_TOKEN,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_SCHEDULED,
    SEED_TODAY,
    iter_seed_customer_ids,
    iter_seed_job_ids,
)


@pytest.fixture()
def backend() -> ServiceTitanFakeBackend:
    """A fresh backend per test, so state mutations (notes) do not leak."""
    return ServiceTitanFakeBackend()


@pytest.fixture()
def client(backend: ServiceTitanFakeBackend) -> httpx.AsyncClient:
    transport = build_fake_transport(backend)
    return httpx.AsyncClient(
        transport=transport,
        base_url="https://api-fake.servicetitan.io",
        headers={"ST-App-Key": backend.app_key},
    )


async def _get_bearer(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_token_endpoint_returns_15_minute_bearer(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == FAKE_ACCESS_TOKEN
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == ACCESS_TOKEN_TTL_SECONDS


@pytest.mark.asyncio()
async def test_token_endpoint_rejects_other_grants(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/connect/token",
        data={
            "grant_type": "password",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


@pytest.mark.asyncio()
async def test_token_endpoint_requires_client_credentials(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/connect/token",
        data={"grant_type": "client_credentials"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


# ---------------------------------------------------------------------------
# Auth on resource endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resource_requires_bearer(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers")
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == 401
    assert "Authorization" in body["detail"]


@pytest.mark.asyncio()
async def test_resource_requires_app_key(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    bearer = await _get_bearer(client)
    # Strip the ST-App-Key header for one request by building a fresh client
    # that only sets Authorization.
    transport = build_fake_transport(backend)
    no_app_key = httpx.AsyncClient(
        transport=transport,
        base_url="https://api-fake.servicetitan.io",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    try:
        resp = await no_app_key.get(f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers")
    finally:
        await no_app_key.aclose()
    assert resp.status_code == 401
    assert "ST-App-Key" in resp.json()["detail"]


@pytest.mark.asyncio()
async def test_expired_token_returns_401(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    bearer = await _get_bearer(client)
    backend.expire_all_tokens()
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_customers_list_shape(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Paginated envelope keys come from the live OpenAPI spec.
    assert set(body.keys()) >= {"page", "pageSize", "hasMore", "totalCount", "data"}
    assert isinstance(body["data"], list)
    assert body["totalCount"] == 10
    # Each record carries the documented fields.
    first = body["data"][0]
    assert {"id", "name", "type", "address", "active", "createdOn", "modifiedOn"} <= set(
        first.keys()
    )
    assert {"street", "city", "state", "zip", "country"} <= set(first["address"].keys())


@pytest.mark.asyncio()
async def test_customers_search_by_name(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        params={"name": "acme"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] == 1
    assert body["data"][0]["name"] == "Acme Plumbing"


@pytest.mark.asyncio()
async def test_customers_search_by_phone(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    # Search using a phone fragment matching one of the seed contacts.
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        params={"phone": "5550101"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    assert body["totalCount"] == 1
    assert body["data"][0]["name"] == "Jane Doe"


@pytest.mark.asyncio()
async def test_customer_get_by_id(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    cust_id = next(iter(iter_seed_customer_ids()))
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers/{cust_id}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == cust_id


@pytest.mark.asyncio()
async def test_customer_get_missing_returns_404(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers/99999999",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == 404


@pytest.mark.asyncio()
async def test_customer_contacts_subresource(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers/1001/contacts",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] >= 1
    contact = body["data"][0]
    assert {"id", "type", "value"} <= set(contact.keys())


@pytest.mark.asyncio()
async def test_customers_pagination(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        params={"page": 1, "pageSize": 3},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    assert body["page"] == 1
    assert body["pageSize"] == 3
    assert len(body["data"]) == 3
    assert body["hasMore"] is True


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_jobs_list_shape(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] == 30
    first = body["data"][0]
    # Documented JpmV2 / CrmV2 JobResponse fields. The nullable cluster
    # (recallForId / warrantyId / leadCallId / bookingId / soldById /
    # jobGeneratedLeadSource) is also serialized by the real API and is
    # asserted here so a seed regression that drops them is loud.
    for field_name in [
        "id",
        "jobNumber",
        "customerId",
        "locationId",
        "jobStatus",
        "businessUnitId",
        "jobTypeId",
        "summary",
        "appointmentCount",
        "recallForId",
        "warrantyId",
        "jobGeneratedLeadSource",
        "leadCallId",
        "bookingId",
        "soldById",
    ]:
        assert field_name in first, f"missing {field_name}"


@pytest.mark.asyncio()
async def test_jobs_filter_by_status(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs",
        params={"jobStatus": f"{JOB_STATUS_SCHEDULED},{JOB_STATUS_COMPLETED}"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    statuses = {j["jobStatus"] for j in body["data"]}
    assert statuses <= {JOB_STATUS_SCHEDULED, JOB_STATUS_COMPLETED}
    assert statuses  # not empty


@pytest.mark.asyncio()
async def test_jobs_filter_by_customer(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs",
        params={"customerId": 1001},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    assert all(j["customerId"] == 1001 for j in body["data"])
    assert body["totalCount"] >= 1


@pytest.mark.asyncio()
async def test_job_get_by_id(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    job_id = next(iter(iter_seed_job_ids()))
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/{job_id}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id


@pytest.mark.asyncio()
async def test_job_get_missing_returns_404(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/99999999",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Job notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_post_job_note_round_trips(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    bearer = await _get_bearer(client)
    job_id = backend.jobs[0]["id"]

    post = await client.post(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/{job_id}/notes",
        json={"text": "Test note: tech arrived on site at 9:05.", "pinToTop": False},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert post.status_code == 200
    body = post.json()
    assert body["text"] == "Test note: tech arrived on site at 9:05."
    assert body["isPinned"] is False
    assert "createdOn" in body

    listing = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/{job_id}/notes",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert listing.status_code == 200
    notes = listing.json()["data"]
    assert any(n["text"].startswith("Test note") for n in notes)


@pytest.mark.asyncio()
async def test_post_job_note_validates_text(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    bearer = await _get_bearer(client)
    job_id = backend.jobs[0]["id"]
    resp = await client.post(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/{job_id}/notes",
        json={"text": "   "},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio()
async def test_post_job_note_missing_job_404(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.post(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/jobs/99999999/notes",
        json={"text": "note"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_appointments_list_shape(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/appointments",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] == 15
    appt = body["data"][0]
    for field_name in [
        "id",
        "jobId",
        "appointmentNumber",
        "start",
        "end",
        "status",
        "technicianIds",
        "createdOn",
        "modifiedOn",
    ]:
        assert field_name in appt
    # ``technicianIds`` is a plural list of int IDs in the real schema.
    assert isinstance(appt["technicianIds"], list)
    assert all(isinstance(t, int) for t in appt["technicianIds"])
    assert appt["technicianIds"], "every appointment should have at least one tech"


@pytest.mark.asyncio()
async def test_appointments_filter_by_date_range(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    day_start = SEED_TODAY.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start.replace(hour=23, minute=59, second=59)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/appointments",
        params={
            "startsOnOrAfter": day_start.isoformat().replace("+00:00", "Z"),
            "startsBefore": day_end.isoformat().replace("+00:00", "Z"),
        },
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    assert body["totalCount"] >= 1
    # Every appointment falls within the window.
    for appt in body["data"]:
        assert appt["start"].startswith(SEED_TODAY.strftime("%Y-%m-%d"))


@pytest.mark.asyncio()
async def test_appointments_filter_by_status(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        f"/jpm/v2/tenant/{DEFAULT_TENANT_ID}/appointments",
        params={"status": f"{APPT_STATUS_SCHEDULED},{APPT_STATUS_DISPATCHED}"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = resp.json()
    seen = {a["status"] for a in body["data"]}
    assert seen <= {APPT_STATUS_SCHEDULED, APPT_STATUS_DISPATCHED}
    assert seen


# ---------------------------------------------------------------------------
# Rate limit override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_force_rate_limit_returns_429(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    bearer = await _get_bearer(client)
    backend.force_rate_limit_for(2)
    first = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert first.status_code == 429
    assert first.headers.get("Retry-After") == "1"
    second = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert second.status_code == 429
    # Third call drains the override and succeeds.
    third = await client.get(
        f"/crm/v2/tenant/{DEFAULT_TENANT_ID}/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert third.status_code == 200


@pytest.mark.asyncio()
async def test_rate_limit_does_not_affect_token_endpoint(
    backend: ServiceTitanFakeBackend, client: httpx.AsyncClient
) -> None:
    backend.force_rate_limit_for(5)
    resp = await client.post(
        "/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unknown tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_other_tenant_returns_empty_collection(client: httpx.AsyncClient) -> None:
    bearer = await _get_bearer(client)
    resp = await client.get(
        "/crm/v2/tenant/9876543/customers",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] == 0
    assert body["data"] == []
