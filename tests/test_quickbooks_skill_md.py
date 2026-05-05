"""Doc-lint tests for the QuickBooks SKILL.md.

The agent treats any field omitted from SKILL.md's "Queryable entities and useful
fields" section as if it does not exist on the entity. That has caused real
regressions (issue #1131: agent claimed QuickBooks does not store the recipient
email on past invoices, because BillEmail was not listed). These tests pin the
field list so future drift surfaces in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "integrations"
    / "quickbooks"
    / "SKILL.md"
)

# Fields each queryable entity must list. Covers the audit in issue #1131:
# anything the agent has been observed to need (or is likely to need) when
# answering questions about prior transactions, customers, items, and bills.
_REQUIRED_QUERYABLE_FIELDS: dict[str, list[str]] = {
    "Invoice": [
        "Id",
        "SyncToken",
        "DocNumber",
        "CustomerRef",
        "TotalAmt",
        "Balance",
        "DueDate",
        "TxnDate",
        "EmailStatus",
        "BillEmail",
        "Line",
        "CustomerMemo",
        "PrivateNote",
        "BillAddr",
        "ShipAddr",
        "LinkedTxn",
    ],
    "Estimate": [
        "Id",
        "SyncToken",
        "DocNumber",
        "CustomerRef",
        "TotalAmt",
        "TxnDate",
        "ExpirationDate",
        "TxnStatus",
        "BillEmail",
        "Line",
        "CustomerMemo",
        "PrivateNote",
        "LinkedTxn",
    ],
    "Customer": [
        "Id",
        "SyncToken",
        "DisplayName",
        "CompanyName",
        "PrimaryEmailAddr",
        "PrimaryPhone",
        "BillAddr",
        "Balance",
        "Active",
        "Notes",
    ],
    "Item": [
        "Id",
        "Name",
        "Sku",
        "Description",
        "UnitPrice",
        "Type",
        "Active",
        "QtyOnHand",
    ],
    "Payment": [
        "Id",
        "CustomerRef",
        "TotalAmt",
        "TxnDate",
        "Line",
    ],
    "Bill": [
        "Id",
        "VendorRef",
        "DocNumber",
        "TotalAmt",
        "DueDate",
        "TxnDate",
        "Line",
    ],
}


def _entity_field_line(content: str, entity: str) -> str:
    """Return the bullet line that lists fields for ``entity``."""
    pattern = re.compile(rf"^- {entity}: (.+)$", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        raise AssertionError(
            f"SKILL.md is missing a queryable-fields bullet for {entity!r}. "
            "Expected a line like `- Invoice: Id, SyncToken, ...`."
        )
    return match.group(1)


@pytest.mark.parametrize(("entity", "fields"), list(_REQUIRED_QUERYABLE_FIELDS.items()))
def test_skill_md_lists_required_queryable_fields(entity: str, fields: list[str]) -> None:
    """Every entity in SKILL.md must list the fields the agent is expected to know about."""
    content = SKILL_MD_PATH.read_text()
    listed = _entity_field_line(content, entity)
    missing = [f for f in fields if not re.search(rf"\b{re.escape(f)}\b", listed)]
    assert not missing, (
        f"SKILL.md's {entity} field list is missing: {missing}. "
        f"Without these, the agent treats the data as 'not stored' and refuses to query for it. "
        f"Current line: {listed}"
    )


def test_skill_md_invoice_lists_billemail() -> None:
    """Regression test for issue #1131.

    The agent answered 'QuickBooks does not store the recipient email on past
    invoices' because BillEmail was not in SKILL.md. Pin it explicitly so
    nobody accidentally drops it.
    """
    content = SKILL_MD_PATH.read_text()
    invoice_line = _entity_field_line(content, "Invoice")
    assert "BillEmail" in invoice_line, (
        "Invoice queryable fields must include BillEmail (the recipient email "
        "address recorded on a sent or unsent invoice). See issue #1131."
    )


def test_skill_md_documents_email_recovery_workflow() -> None:
    """SKILL.md must describe how to recover a customer email before asking the user.

    qb_send needs a recipient email. If SKILL.md does not document the
    PrimaryEmailAddr -> Invoice.BillEmail fallback, the agent will either
    interrupt the user for an email it could have looked up, or claim the
    data is unavailable. See issue #1131.
    """
    content = SKILL_MD_PATH.read_text()
    assert "Recovering a customer email" in content, (
        "SKILL.md should include a 'Recovering a customer email' workflow under Common Workflows."
    )
    # The workflow must reference both the Customer-level and Invoice-level fallbacks
    # so the agent knows where to look before asking.
    assert "PrimaryEmailAddr" in content
    assert "BillEmail" in content
