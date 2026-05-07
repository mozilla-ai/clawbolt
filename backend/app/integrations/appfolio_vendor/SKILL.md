# AppFolio Vendor Portal

Read-only access to the user's AppFolio Vendor Portal: work orders,
payments AppFolio has issued, and vendor profile. Write tools (notes,
invoices, photos) ship in a follow-up.

## Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| appfolio_connect | First-time connect with a pasted magic link | Auto |
| appfolio_complete_2fa | Submit a 2FA code if AppFolio asks for one | Auto |
| appfolio_list_work_orders | List work orders, filter by status | Auto |
| appfolio_search_work_orders | Search by address, number, or text | Auto |
| appfolio_get_work_order | One work order's details | Auto |
| appfolio_list_payments | Payments AppFolio has issued | Auto |
| appfolio_get_profile | Connected vendor profile | Auto |

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
