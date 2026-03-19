# QuickBooks Online

You now have access to QuickBooks Online tools. Here is how to use them effectively.

## Available Tools

| Tool | Purpose |
|------|---------|
| `qb_query` | Run read-only queries using QBO query language |
| `qb_create` | Create a Customer, Estimate, or Invoice |
| `qb_send` | Email an invoice to a customer |

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

## Sending Invoices (qb_send)

- You need the invoice ID (numeric) and the recipient email address.
- Confirm the email address with the user before sending.

## Common Workflows

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
