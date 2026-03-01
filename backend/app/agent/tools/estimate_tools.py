"""Estimate generation tools for the agent."""

import datetime
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.models import Contractor, Estimate, EstimateLineItem
from backend.app.services.pdf_service import EstimatePDFData, generate_estimate_pdf

PDF_DIR = Path("data/estimates")
ESTIMATE_NUMBER_FORMAT = "EST-{:04d}"

logger = logging.getLogger(__name__)


class EstimateStatus:
    DRAFT = "draft"
    SENT = "sent"


def _next_estimate_number(db: Session, contractor_id: int) -> str:
    """Generate the next sequential estimate number for a contractor."""
    count = db.query(Estimate).filter(Estimate.contractor_id == contractor_id).count()
    return ESTIMATE_NUMBER_FORMAT.format(count + 1)


def create_estimate_tools(
    db: Session,
    contractor: Contractor,
) -> list[Tool]:
    """Create estimate-related tools for the agent."""

    async def generate_estimate(
        description: str,
        line_items: list[dict[str, object]],
        client_name: str | None = None,
        client_address: str | None = None,
        terms: str | None = None,
    ) -> str:
        """Generate a professional estimate PDF and return summary."""
        estimate_number = _next_estimate_number(db, contractor.id)
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

        # Calculate totals
        processed_items: list[dict[str, object]] = []
        subtotal = 0.0
        for item in line_items:
            qty = float(item.get("quantity", 1))
            price = float(item.get("unit_price", 0))
            total = qty * price
            subtotal += total
            processed_items.append(
                {
                    "description": str(item.get("description", "")),
                    "quantity": qty,
                    "unit_price": price,
                    "total": total,
                }
            )

        total_amount = subtotal

        # Create database records
        estimate = Estimate(
            contractor_id=contractor.id,
            description=description,
            total_amount=total_amount,
            status=EstimateStatus.DRAFT,
        )
        db.add(estimate)
        db.flush()  # Get the ID before adding line items

        for item in processed_items:
            line_item = EstimateLineItem(
                estimate_id=estimate.id,
                description=str(item["description"]),
                quantity=float(item["quantity"]),
                unit_price=float(item["unit_price"]),
                total=float(item["total"]),
            )
            db.add(line_item)

        db.commit()
        db.refresh(estimate)

        # Generate PDF
        pdf_data = EstimatePDFData(
            contractor_name=contractor.name or "Contractor",
            contractor_phone=contractor.phone or "",
            contractor_trade=contractor.trade or "",
            description=description,
            line_items=processed_items,
            subtotal=subtotal,
            total=total_amount,
            estimate_date=today,
            estimate_number=estimate_number,
            client_name=client_name,
            client_address=client_address,
            terms=terms or "Payment due within 30 days of project completion.",
        )

        pdf_bytes = await generate_estimate_pdf(pdf_data)

        # Save PDF to local storage
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = PDF_DIR / f"{estimate.id}.pdf"
        pdf_path.write_bytes(pdf_bytes)

        # Update estimate with PDF URL (served by /api/estimates/{id}/pdf)
        estimate.pdf_url = f"/api/estimates/{estimate.id}/pdf"
        estimate.status = EstimateStatus.SENT
        db.commit()

        return (
            f"Estimate {estimate_number} generated for ${total_amount:,.2f}. "
            f"{len(processed_items)} line item(s). "
            f"PDF available at {estimate.pdf_url}"
        )

    return [
        Tool(
            name="generate_estimate",
            description=(
                "Generate a professional estimate PDF. Use when the contractor asks for "
                "an estimate, quote, or bid. Include line items with description, quantity, "
                "and unit_price."
            ),
            function=generate_estimate,
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Brief description of the work",
                    },
                    "line_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "quantity": {"type": "number", "default": 1},
                                "unit_price": {"type": "number"},
                            },
                            "required": ["description", "unit_price"],
                        },
                        "description": "Line items for the estimate",
                    },
                    "client_name": {
                        "type": "string",
                        "description": "Client name (optional)",
                    },
                    "client_address": {
                        "type": "string",
                        "description": "Client address (optional)",
                    },
                    "terms": {
                        "type": "string",
                        "description": "Payment terms (optional)",
                    },
                },
                "required": ["description", "line_items"],
            },
        ),
    ]
