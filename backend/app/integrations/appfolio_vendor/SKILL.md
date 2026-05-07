# AppFolio Vendor Portal

You can manage the user's AppFolio Vendor Portal account: view assigned
work orders, search by address or work order number, check on payments,
and read their vendor profile. (Write tools, including invoice creation
with photo attachments, ship in a follow-up.)

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| appfolio_connect | First-time connect with a pasted magic link | Auto |
| appfolio_complete_2fa | Submit a 2FA code if AppFolio asks for one | Auto |
| appfolio_list_work_orders | List open work orders, filter by status | Auto |
| appfolio_search_work_orders | Search by address, work order number, or text | Auto |
| appfolio_get_work_order | Drill into one work order's details | Auto |
| appfolio_list_payments | List recent payments, filter by date or method | Auto |
| appfolio_get_profile | Read the connected vendor profile | Auto |

## Connecting (first time)

AppFolio uses passwordless magic-link login. The flow is:

1. Tell the user: open vendor.appfolio.com in their browser, enter their
   email, and request the magic link.
2. They will receive an email with a link like
   `https://vendor.appfolio.com/?magic_link_token=eyJ...`. Ask them to
   paste the **full** URL back to you, not just the token.
3. Call `appfolio_connect(magic_link="<the URL>")`.
4. If the response says 2FA is required, AppFolio will SMS or email a
   short numeric code. Ask the user for the code and call
   `appfolio_complete_2fa(code="...")`.

Magic links are single-use and expire quickly (minutes). If the
exchange fails, just have the user request a fresh link.

## Workflow: morning work-order check

When the user asks "what work orders do I have", "what's on my plate",
or similar:

1. Call `appfolio_list_work_orders()` (defaults: in-progress + estimates).
2. Group by urgency in your reply: estimates needed first (they often
   block payment), then oldest in-progress.
3. If they ask about one job specifically, run
   `appfolio_search_work_orders` with their phrasing, then
   `appfolio_get_work_order` for the match.

## Workflow: payment lookup

When the user asks "did I get paid for the Smith job", "any payments
this week", or similar:

1. `appfolio_list_payments(posted_on="<YYYY-MM-DD or empty>")`.
2. If they specify a method (instant pay, e-check, paper check), pass
   `settlement_method` accordingly: `push_to_debit`, `e_check`, or
   `bill_pay_check`.
3. To trace one payment back to a work order, search the work orders
   with the same property address.

## Session expiry

When any tool returns an `auth` error, AppFolio rejected the JWT. Tell
the user the session expired and ask them to:

1. Open vendor.appfolio.com and request a new magic link.
2. Paste it back to you.
3. You re-run `appfolio_connect`.

Their fingerprint is preserved across reconnects, so AppFolio will
recognize the same client.

## Multiple property managers

A vendor often works for several property managers (each is a "customer"
in AppFolio terms). Work-order list output mixes customers by default;
filter to one by passing `customer_id` to `appfolio_list_work_orders`.
The IDs come from the `appfolio_get_profile` output or from the work
order list itself.
