# AppFolio Vendor Portal

Full read/write access to the user's AppFolio Vendor Portal: work
orders, notes with photos, scheduling, status updates, tenant
messaging, invoices (line items or PDF), compliance docs, estimates,
payments, and profile.

## Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| appfolio_connect | First-time connect with a pasted magic link | Auto |
| appfolio_complete_2fa | Submit a 2FA code if AppFolio asks for one | Auto |
| appfolio_list_work_orders | List work orders, filter by status | Auto |
| appfolio_search_work_orders | Search by address, number, or text | Auto |
| appfolio_get_work_order | One work order's details | Auto |
| appfolio_accept_work_order | Accept a work order assignment | Ask |
| appfolio_schedule_work_order | Set the scheduled visit time | Ask |
| appfolio_update_work_order_status | Update the status code | Ask |
| appfolio_undo_work_order_status | Revert a recent status change | Ask |
| appfolio_list_notes | List notes on a work order | Auto |
| appfolio_add_note | Add a note (with optional photos) | Ask |
| appfolio_update_note | Edit an existing note | Ask |
| appfolio_message_tenant | SMS the tenant via AppFolio's proxy | Ask |
| appfolio_list_payments | Payments AppFolio has issued | Auto |
| appfolio_get_profile | Connected vendor profile | Auto |
| appfolio_update_profile | Update profile fields | Ask |
| appfolio_create_invoice | Build a line-itemized invoice with photos | Ask |
| appfolio_upload_invoice_pdf | Upload a pre-built invoice PDF | Ask |
| appfolio_upload_compliance_doc | Upload W-9, COI, license, etc. | Ask |
| appfolio_get_estimate | Get an estimate's details | Auto |
| appfolio_update_estimate | Update an estimate's amount or description | Ask |

## Photos and documents

`appfolio_add_note`, `appfolio_update_note`, `appfolio_create_invoice`,
`appfolio_upload_invoice_pdf`, and `appfolio_upload_compliance_doc`
all accept media references. Each entry is either an `original_url`
from a sent image or a media handle (e.g. `media_xxxx`) returned by
`analyze_photo`. AppFolio receives them inline as base64 in the JSON
body.

## Connecting

AppFolio uses passwordless magic-link login (single-use, expires in
minutes):

1. User opens vendor.appfolio.com and requests a magic link.
2. They paste the full URL from the email back to you.
3. Call `appfolio_connect(magic_link=...)`.
4. If it reports 2FA is required, ask the user for the SMS or email
   code and call `appfolio_complete_2fa`.

On `auth` errors (expired session) the same flow gets the user a fresh
JWT; their fingerprint persists.
