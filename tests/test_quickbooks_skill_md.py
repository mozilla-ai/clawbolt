"""Doc-lint tests for the QuickBooks SKILL.md.

Covers two related regression classes:
- Issue #1131: agent claims a field does not exist because SKILL.md does not
  list it (e.g. BillEmail on Invoice). The field-list pinning tests prevent
  that drift.
- Issue #1403: agent claims a customer, invoice, or estimate is absent
  without calling ``qb_query`` first. The guard tests prevent that drift.
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


# --- #1403 guard tests --------------------------------------------------------

def test_skill_md_has_finding_entity_section() -> None:
    """SKILL.md must carry a dedicated 'Finding a customer, invoice, or estimate' guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding a customer, invoice, or estimate" in content, (
        "SKILL.md should include a 'Finding a customer, invoice, or estimate' section "
        "instructing the agent to query before claiming an entity is absent."
    )


def test_skill_md_requires_query_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before querying.

    Without this, the agent answers from its in-context entity cache: a name
    it has not queried this session reads as 'does not exist', so it creates a
    duplicate of a customer that is already there, or claims an invoice does
    not exist.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The two tools the guard ties together.
    assert "qb_query" in content
    assert "qb_create" in content
    # The core framing: not-queried is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unqueried entity is 'unknown, not "
        "absent' so the agent queries before claiming it does not exist."
    )


def test_skill_md_new_customer_job_queries_first() -> None:
    """The 'New customer job' workflow must query Customer before creating.

    The original workflow jumped straight to qb_create Customer, risking a
    duplicate. Reinforce that the agent searches first.
    """
    content = SKILL_MD_PATH.read_text()
    assert "qb_query" in content, (
        "The New customer job workflow should include a qb_query step to "
        "check if the customer exists before creating."
    )
