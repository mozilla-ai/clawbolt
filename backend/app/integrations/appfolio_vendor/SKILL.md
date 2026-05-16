# AppFolio Vendor Portal

Read work orders and notes, update work-order status (e.g. mark
complete), add notes (with photos), and create invoices on the user's
AppFolio Vendor Portal.

## Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| appfolio_connect | First-time connect with a pasted magic link | Auto |
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

## Photos and documents

`appfolio_add_note`, `appfolio_update_note`, `appfolio_create_invoice`,
and `appfolio_upload_invoice_pdf` all accept media references. Each
entry is either an `original_url` from a sent image or a media handle
(e.g. `media_xxxx`) returned by `analyze_photo`. AppFolio receives
them inline as base64 in the JSON body.

## Connecting

AppFolio uses passwordless magic-link login (single-use, expires in
minutes):

1. User opens vendor.appfolio.com and requests a magic link.
2. From the email, they copy only the token from the magic-link URL
   (everything after `magic_link_token=`), not the full URL. iMessage
   and other SMS clients strip query params from pasted links.
3. Call `appfolio_connect(magic_link=...)` with the pasted token.

The OAuth2 exchange returns a refresh token alongside the bearer JWT,
so live sessions extend automatically on 401. On `auth` errors with no
refresh path left, the same flow gets the user a fresh JWT; their
fingerprint persists.
