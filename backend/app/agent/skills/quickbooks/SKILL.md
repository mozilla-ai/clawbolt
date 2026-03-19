# QuickBooks Online

You now have access to QuickBooks Online tools. Here is how to use them effectively.

## Available Tools

| Tool | Purpose |
|------|---------|
| `qb_query` | Run read-only queries using QBO query language |
| `qb_create_estimate` | Create an estimate for a customer |
| `qb_create_invoice` | Create an invoice for a customer |
| `qb_create_customer` | Create a new customer |
| `qb_send_invoice` | Email an invoice to a customer |
| `qb_estimate_to_invoice` | Convert an estimate into an invoice |

## Query Guide (qb_query)

### Queryable entities and useful fields
- Invoice: Id, DocNumber, CustomerRef, TotalAmt, Balance, DueDate, TxnDate, EmailStatus
- Estimate: Id, DocNumber, CustomerRef, TotalAmt, TxnDate, ExpirationDate, TxnStatus
- Customer: Id, DisplayName, PrimaryEmailAddr, PrimaryPhone, Balance
- Item: Id, Name, Description, UnitPrice, Type
- Payment: Id, CustomerRef, TotalAmt, TxnDate
- Bill: Id, VendorRef, TotalAmt, DueDate, Balance

### Syntax
SELECT <fields> FROM <Entity> [WHERE <conditions>] [ORDERBY <field> DESC] [MAXRESULTS <n>]

### Operators
=, <, >, <=, >=, LIKE '%text%', IN ('a','b')

### Tips
- No subqueries. To filter by customer name, first query Customer to get the Id, then use CustomerRef = '<id>' in a second query.
- Always use MAXRESULTS to keep results manageable.

## Creating Estimates and Invoices

- The customer must already exist in QuickBooks. Look them up first with qb_query if unsure.
- Provide line items with description, quantity, and unit price.
- Optionally set an expiration date (estimates) or due date (invoices) in YYYY-MM-DD format.
- Optionally add a memo/notes field.

## Creating Customers

- The display name must be unique in QuickBooks.
- Optionally provide email and phone.

## Sending Invoices

- You need the invoice ID and the recipient email address.
- Confirm the email address with the user before sending.

## Converting Estimates to Invoices

- Creates a new invoice linked to the original estimate.
- Carries over line items and customer info automatically.

## Common Workflows

1. **New customer job**: qb_create_customer -> qb_create_estimate -> (user approves) -> qb_estimate_to_invoice -> qb_send_invoice
2. **Quick invoice**: qb_query Customer (get ID) -> qb_create_invoice -> qb_send_invoice
3. **Check balances**: qb_query "SELECT * FROM Invoice WHERE Balance > '0'"
