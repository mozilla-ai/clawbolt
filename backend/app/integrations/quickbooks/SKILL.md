# QuickBooks Online

You now have access to QuickBooks Online tools. Here is how to use them effectively.

## Available Tools

| Tool | Purpose |
|------|---------|
| `qb_query` | Run read-only queries using QBO query language |
| `qb_create` | Create a Customer, Estimate, or Invoice |
| `qb_update` | Update an existing Customer, Estimate, or Invoice |
| `qb_send` | Email an invoice or estimate to a customer |

## Query Guide (qb_query)

### Queryable entities and useful fields
- Invoice: Id, SyncToken, DocNumber, CustomerRef, TotalAmt, Balance, DueDate, TxnDate, EmailStatus
- Estimate: Id, SyncToken, DocNumber, CustomerRef, TotalAmt, TxnDate, ExpirationDate, TxnStatus
- Customer: Id, SyncToken, DisplayName, PrimaryEmailAddr, PrimaryPhone, Balance
- Item: Id, Name, Description, UnitPrice, Type
- Payment: Id, CustomerRef, TotalAmt, TxnDate
- Bill: Id, VendorRef, TotalAmt, DueDate, Balance

SyncToken is returned in query results; you need it when updating an entity with `qb_update`.

### Syntax
SELECT <fields> FROM <Entity> [WHERE <conditions>] [ORDERBY <field> DESC] [MAXRESULTS <n>]

### Operators
=, <, >, <=, >=, LIKE '%text%', IN ('a','b')

### Tips
- No subqueries. To filter by customer name, first query Customer to get the Id, then use CustomerRef = '<id>' in a second query.
- Always use MAXRESULTS to keep results manageable.
- Not all fields support all operators. For example, Estimate TxnStatus does not support IN or LIKE. If a query returns a 400 error, simplify the WHERE clause and filter results yourself.
- String comparisons are case-sensitive in QBO queries.

## Creating Entities (qb_create)

Pass `entity_type` (Customer, Estimate, or Invoice) and `data` (the QBO API payload).

### Customer payload

Required fields:
- `DisplayName` (string, must be unique in QB)

Optional fields:
- `PrimaryEmailAddr`: `{"Address": "email@example.com"}`
- `PrimaryPhone`: `{"FreeFormNumber": "555-1234"}`
- `CompanyName`: string
- `GivenName`, `FamilyName`: strings
- `BillAddr`: `{"Line1": "...", "City": "...", "CountrySubDivisionCode": "CA", "PostalCode": "90210"}`

Example:
```json
{
  "entity_type": "Customer",
  "data": {
    "DisplayName": "Jane Smith",
    "PrimaryEmailAddr": {"Address": "jane@example.com"},
    "PrimaryPhone": {"FreeFormNumber": "555-0199"}
  }
}
```

### Estimate payload

Required fields:
- `CustomerRef`: `{"value": "<customer_id>"}` (look up the customer first with qb_query)
- `Line`: array of line items (see below)

Optional fields:
- `ExpirationDate`: "YYYY-MM-DD"
- `CustomerMemo`: `{"value": "notes text"}`
- `TxnDate`: "YYYY-MM-DD" (defaults to today)

### Invoice payload

Required fields:
- `CustomerRef`: `{"value": "<customer_id>"}`
- `Line`: array of line items (see below)

Optional fields:
- `DueDate`: "YYYY-MM-DD"
- `CustomerMemo`: `{"value": "notes text"}`
- `TxnDate`: "YYYY-MM-DD" (defaults to today)
- `LinkedTxn`: array of linked transactions (used when converting an estimate)

### Line item format

Each line item in the `Line` array should look like:
```json
{
  "Amount": 400.00,
  "DetailType": "SalesItemLineDetail",
  "Description": "Labor - kitchen remodel",
  "SalesItemLineDetail": {
    "Qty": 8,
    "UnitPrice": 50.00
  }
}
```

`Amount` should equal `Qty * UnitPrice`.

## Updating Entities (qb_update)

Pass `entity_type` and `data` with the **full entity payload including Id and SyncToken** from a prior `qb_query`.

The SyncToken is required for optimistic concurrency. If the entity was modified since you last queried it, QuickBooks will reject the update with a conflict error. In that case, re-query the entity and try again with the new SyncToken.

### Update example
```json
{
  "entity_type": "Estimate",
  "data": {
    "Id": "2001",
    "SyncToken": "0",
    "CustomerRef": {"value": "100"},
    "Line": [
      {
        "Amount": 600.00,
        "DetailType": "SalesItemLineDetail",
        "Description": "Labor - kitchen remodel (revised)",
        "SalesItemLineDetail": {"Qty": 12, "UnitPrice": 50.00}
      },
      {
        "Amount": 350.00,
        "DetailType": "SalesItemLineDetail",
        "Description": "Materials",
        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 350.00}
      }
    ]
  }
}
```

## Sending Invoices and Estimates (qb_send)

- Pass `entity_type` (Invoice or Estimate), the entity ID (numeric), and the recipient email address.
- Confirm the email address with the user before sending.

## Propose-then-veto

When the user dictates a job with partial details, do not enumerate the missing fields. Fill in reasonable defaults from context (USER.md pricing, similar past jobs in MEMORY.md, common-case assumptions) and create the draft. The user can edit one thing faster than they can answer a list of questions.

Hard requirements where you must confirm before acting: the recipient email on `qb_send`, and any destructive update that would overwrite line items the user has not seen. Everything else (line item rates, quantities, expiration dates, customer memo wording) should be drafted with a sensible default and surfaced in one short summary line.

## Common Workflows

### Voice-to-estimate (dictation workflow)
This is the primary workflow for users who dictate job details from the field:
1. User describes a job (client, scope, labor, materials) via chat
2. Extract structured data from the description, filling in defaults for anything the user did not specify (rate, quantity, expiration date, memo)
3. `qb_query` Customer to check if the client exists
4. If new client: `qb_create` Customer
5. `qb_create` Estimate with line items (typically labor + materials)
6. Summarize what you drafted and what you assumed in one line: "Drafted estimate for Smith: 8 hr labor at $50, materials $200, expires in 30 days. Change anything?"
7. User comes back later to refine: `qb_query` the estimate (note the SyncToken in the results)
8. `qb_update` Estimate with revised line items (include Id and SyncToken)
9. When user says it's ready: `qb_send` Estimate to the client's email

### New customer job
1. `qb_create` Customer
2. `qb_create` Estimate with the new customer's Id
3. User approves the estimate
4. Convert estimate to invoice (see below)
5. `qb_send` the invoice

### Quick invoice
1. `qb_query` Customer to get the customer Id
2. `qb_create` Invoice with CustomerRef and line items
3. `qb_send` the invoice

### Convert estimate to invoice
1. `qb_query`: `SELECT * FROM Estimate WHERE Id = '<estimate_id>'`
2. `qb_create` Invoice using the estimate's CustomerRef and Line items, plus a LinkedTxn:
```json
{
  "entity_type": "Invoice",
  "data": {
    "CustomerRef": {"value": "<customer_id from estimate>"},
    "Line": [... line items from estimate ...],
    "LinkedTxn": [{"TxnId": "<estimate_id>", "TxnType": "Estimate"}]
  }
}
```
QuickBooks automatically updates the estimate status when a linked invoice is created.

### Check outstanding balances
`qb_query`: `SELECT * FROM Invoice WHERE Balance > '0' MAXRESULTS 20`
