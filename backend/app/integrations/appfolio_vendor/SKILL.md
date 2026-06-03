# AppFolio Vendor Portal

Read work orders and notes, update work-order status (e.g. mark
complete), add notes (with photos), and create invoices on the user's
AppFolio Vendor Portal.

## Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| appfolio_list_work_orders | List work orders, filter by status | Auto |
| appfolio_search_work_orders | Search by address, number, or text | Auto |
| appfolio_get_work_order | One work order's details | Auto |
| appfolio_update_work_order_status | Update the status code (e.g. mark complete) | Ask |
| appfolio_undo_work_order_status | Revert a recent status change | Ask |
| appfolio_list_notes | List notes on a work order | Auto |
| appfolio_add_note | Add a note (with optional photos) | Ask |
| appfolio_update_note | Edit an existing note | Ask |
| appfolio_create_invoice | Build a line-itemized invoice with photos | Ask |
| appfolio_upload_invoice_pdf | Upload a pre-built invoice PDF | Ask |

Common status codes: `0` = new, `4` = in progress, `8` = completed.
Confirm with the user when uncertain rather than guessing.

## Finding a work order

A work order you have not searched this session is unknown, not absent. Never
tell the user a work order does not exist until
`appfolio_search_work_orders` has returned no match for the address, number,
or text they gave. A work order ID you already resolved this session can be
reused without re-searching.

## Photos and documents

See the ``analyze_photo`` tool description for the ``media_XXXXXX`` handle
convention. AppFolio receives photo bytes inline as base64 in the JSON body.

## Connecting

AppFolio uses passwordless magic-link login. The magic link is a single-use
secret, so the user connects in the Clawbolt web app under Settings,
Integrations, never over chat where it would stay in the message history:

1. User opens vendor.appfolio.com and requests a magic link.
2. They open the Clawbolt web app, go to Settings, Integrations, AppFolio
   Vendor Portal, and paste the magic link from their AppFolio email there.

Direct the user to the web app; do not accept the magic link in the
conversation. The OAuth2 exchange returns a refresh token alongside the
bearer JWT, so live sessions extend automatically on 401. When the refresh
path is exhausted, the user reconnects the same way; their fingerprint
persists.
