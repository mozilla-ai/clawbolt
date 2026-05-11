# ServiceTitan

ServiceTitan is a field-service management platform for HVAC, plumbing, and electrical trades. Customers, jobs, appointments, estimates, and invoices live in the tenant; this integration currently surfaces customers and appointments as read-only.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| `st_search_customers` | Find customers by name or phone substring | Auto |
| `st_get_customer` | Fetch one customer record by numeric id | Auto |
| `st_list_appointments` | List appointments in a date window, optionally by status | Auto |

## Entity vocabulary

- **Customer**: the billable party. Has `id`, `name`, `type`, `address`, `contacts`, `balance`, and flags (`active`, `doNotMail`, `doNotService`).
- **Job**: a unit of work for a customer. Returned indirectly via `appointment.jobId`. Not directly queryable through these tools.
- **Appointment**: a scheduled visit on a job. Has `id`, `jobId`, `start`, `end`, `status`, `technicianIds`. One job typically has one appointment; recalls and multi-visit work generate additional appointments tied to the same `jobId`.

Appointment status values: `Scheduled`, `Dispatched`, `Working`, `Done`, `Hold`.

## Dates

All date inputs to `st_list_appointments` are ISO 8601. Append `Z` for UTC (`2026-05-11T00:00:00Z`) or use a local-offset suffix (`2026-05-11T08:00:00-04:00`). Omitting both `from_date` and `to_date` defaults to today (UTC, midnight to midnight); pass an explicit window for any other range.

## Connecting

ServiceTitan auth is OAuth2 client credentials, not a browser flow. The user pastes three values from ServiceTitan Settings, Integrations, API Application Access:

1. Tenant ID
2. Client ID
3. Client Secret

Then call `connect_servicetitan(tenant_id=..., client_id=..., client_secret=...)`. Until that runs, the data tools stay surfaced under "Not connected" in `list_capabilities` and refuse to execute.

## Common Workflows

### Customer just called

1. `st_search_customers(query="<name or phone fragment>")`. The tool routes numeric queries to ServiceTitan's phone filter automatically; alphabetic queries go to the name filter.
2. If multiple matches, narrow the query or ask the user. If none, confirm spelling before reporting "no customer found"; the tenant may use a business name instead of a personal one.
3. `st_get_customer(customer_id=<id>)` for the full record (address, contacts, balance, flags). Check `doNotService` before promising work.

### What's my day

1. `st_list_appointments()` with no arguments. Returns today's appointments sorted by start time.
2. Group the output by status (`Scheduled` / `Dispatched` first, then `Working`, then `Done` / `Hold`) when summarizing for a coordinator.
3. For a specific window (this week, tomorrow), pass `from_date` and `to_date` explicitly.

### Status-filtered dispatch view

`st_list_appointments(status="Scheduled")` for today's unstarted work, or pair with `from_date` and `to_date` to audit a past day for missed `Hold` entries.

## Companion integrations

- **QuickBooks**: use for invoice and estimate workflows. ServiceTitan customer ids are not QuickBooks customer ids; match on name or phone when crossing layers.
- **Google Calendar**: ServiceTitan appointments are the source of truth for scheduled work. If the user wants a calendar event mirroring an appointment, build it from the appointment fields rather than treating Calendar as the schedule.
- **CompanyCam**: photo evidence for a ServiceTitan job lives in a CompanyCam project keyed by the customer's address; resolve the address via `st_get_customer` before searching CompanyCam.
