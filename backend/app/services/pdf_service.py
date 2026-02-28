import asyncio
import io
from dataclasses import dataclass

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


@dataclass
class EstimatePDFData:
    contractor_name: str
    contractor_phone: str
    contractor_trade: str
    description: str
    line_items: list[dict[str, object]]
    subtotal: float
    total: float
    estimate_date: str
    estimate_number: str
    client_name: str | None = None
    client_address: str | None = None
    tax_rate: float | None = None
    tax_amount: float | None = None
    terms: str | None = None


def _build_pdf(data: EstimatePDFData) -> bytes:
    """Build the PDF synchronously."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    elements: list[object] = []

    title_style = ParagraphStyle(
        "EstimateTitle",
        parent=styles["Title"],
        fontSize=24,
        spaceAfter=12,
    )
    elements.append(Paragraph("ESTIMATE", title_style))
    elements.append(Spacer(1, 12))

    # Contractor info
    info_style = styles["Normal"]
    elements.append(Paragraph(f"<b>{data.contractor_name}</b>", info_style))
    if data.contractor_trade:
        elements.append(Paragraph(data.contractor_trade, info_style))
    if data.contractor_phone:
        elements.append(Paragraph(data.contractor_phone, info_style))
    elements.append(Spacer(1, 12))

    # Date and estimate number
    elements.append(Paragraph(f"Date: {data.estimate_date}", info_style))
    elements.append(Paragraph(f"Estimate #: {data.estimate_number}", info_style))
    elements.append(Spacer(1, 12))

    # Client info
    if data.client_name:
        elements.append(Paragraph(f"<b>For:</b> {data.client_name}", info_style))
    if data.client_address:
        elements.append(Paragraph(data.client_address, info_style))
    if data.client_name or data.client_address:
        elements.append(Spacer(1, 12))

    # Description
    if data.description:
        elements.append(Paragraph(f"<b>Description:</b> {data.description}", info_style))
        elements.append(Spacer(1, 12))

    # Line items table
    table_data: list[list[str]] = [["Description", "Qty", "Rate", "Total"]]
    for item in data.line_items:
        table_data.append(
            [
                str(item.get("description", "")),
                str(item.get("quantity", 1)),
                f"${item.get('unit_price', 0):,.2f}",
                f"${item.get('total', 0):,.2f}",
            ]
        )

    col_widths = [3.5 * inch, 0.75 * inch, 1.25 * inch, 1.25 * inch]
    table = Table(table_data, colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 12))

    # Totals
    totals_data: list[list[str]] = [["Subtotal:", f"${data.subtotal:,.2f}"]]
    if data.tax_rate is not None and data.tax_amount is not None:
        totals_data.append([f"Tax ({data.tax_rate}%):", f"${data.tax_amount:,.2f}"])
    totals_data.append(["Total:", f"${data.total:,.2f}"])

    totals_table = Table(totals_data, colWidths=[5.0 * inch, 1.75 * inch])
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (-1, -1), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(totals_table)

    # Terms
    if data.terms:
        elements.append(Spacer(1, 24))
        elements.append(Paragraph("<b>Terms:</b>", info_style))
        elements.append(Paragraph(data.terms, info_style))

    doc.build(elements)
    return buf.getvalue()


async def generate_estimate_pdf(data: EstimatePDFData) -> bytes:
    """Generate a professional estimate PDF. Returns PDF file as bytes."""
    return await asyncio.to_thread(_build_pdf, data)
