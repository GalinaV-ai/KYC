"""
Fake document generator for the adversary agent.

Generates realistic-looking PDF documents (invoices, contracts, agreements)
based on the fraudster's legend. Completely isolated — no imports from agents/.

Uses reportlab for PDF generation.
"""

import os
import json
import tempfile
from datetime import datetime, timedelta
from typing import Optional

from openai import OpenAI

# ─── Model ───
MODEL = "gpt-5.4"


def _ensure_reportlab():
    """Lazy import reportlab, install if missing."""
    try:
        import reportlab
        return True
    except ImportError:
        os.system("pip install reportlab --break-system-packages -q")
        return True


# ─── Document content generation via LLM ───

DOCUMENT_CONTENT_PROMPT = """You are generating REALISTIC fake document content for testing purposes.

The document is: {doc_type}
Business details:
{legend_summary}

Context from the conversation (what the interviewer asked for):
{context}

Generate the document content as a JSON object with these fields:
{{
  "title": "Document title (e.g. 'INVOICE', 'SERVICE AGREEMENT')",
  "header": "Company name / letterhead text",
  "reference": "Document reference number",
  "date": "Document date (use realistic recent dates)",
  "from_entity": "Who is sending/issuing (name and address)",
  "to_entity": "Who is receiving (name and address)",
  "body_lines": ["Line 1 of content", "Line 2...", ...],
  "table_rows": [["Description", "Qty", "Unit Price", "Total"], ["Item 1", "1", "£500", "£500"]],
  "footer_lines": ["Payment terms", "Bank details...", "Signed: ..."],
  "total_amount": "£X,XXX.XX"
}}

Make it look REAL — proper formatting, plausible amounts matching the business type, professional language.
Return ONLY valid JSON."""


def generate_document_content(
    doc_type: str, legend: dict, context: str = ""
) -> dict:
    """Use LLM to generate realistic document content."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)

    legend_summary = (
        f"Company: {legend.get('company_name', '?')}\n"
        f"Business: {legend.get('business_type', '?')}\n"
        f"Owner: {legend.get('full_name', '?')}\n"
        f"Revenue: {legend.get('annual_revenue', '?')}\n"
        f"Address: {legend.get('address', '?')}\n"
        f"Description: {legend.get('business_description', '?')}\n"
        f"Clients: {', '.join(legend.get('key_clients', []))}\n"
        f"Suppliers: {', '.join(legend.get('key_suppliers', []))}"
    )

    prompt = DOCUMENT_CONTENT_PROMPT.format(
        doc_type=doc_type,
        legend_summary=legend_summary,
        context=context,
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Generate realistic document content. Return ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    return json.loads(text)


# ─── PDF rendering ───

def render_pdf(doc_content: dict, output_dir: str) -> str:
    """Render document content to a PDF file. Returns the file path."""
    _ensure_reportlab()

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    title = doc_content.get("title", "DOCUMENT")
    ref = doc_content.get("reference", "DOC-001")
    safe_ref = ref.replace("/", "-").replace(" ", "_")
    filename = f"{safe_ref}.pdf"
    filepath = os.path.join(output_dir, filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle", parent=styles["Heading1"],
        fontSize=18, spaceAfter=6, alignment=1,
    )
    header_style = ParagraphStyle(
        "DocHeader", parent=styles["Heading2"],
        fontSize=12, spaceAfter=4, textColor=colors.HexColor("#333333"),
    )
    body_style = ParagraphStyle(
        "DocBody", parent=styles["Normal"],
        fontSize=10, spaceAfter=3, leading=14,
    )
    small_style = ParagraphStyle(
        "DocSmall", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#666666"), spaceAfter=2,
    )

    elements = []

    # Header / company name
    header = doc_content.get("header", "")
    if header:
        elements.append(Paragraph(header, header_style))
        elements.append(Spacer(1, 4*mm))

    # Title
    elements.append(Paragraph(title, title_style))
    elements.append(Spacer(1, 2*mm))

    # Reference and date
    date_str = doc_content.get("date", datetime.now().strftime("%d/%m/%Y"))
    elements.append(Paragraph(f"<b>Ref:</b> {ref} &nbsp;&nbsp;&nbsp; <b>Date:</b> {date_str}", body_style))
    elements.append(Spacer(1, 4*mm))

    # From / To
    from_entity = doc_content.get("from_entity", "")
    to_entity = doc_content.get("to_entity", "")
    if from_entity:
        elements.append(Paragraph(f"<b>From:</b> {from_entity}", body_style))
    if to_entity:
        elements.append(Paragraph(f"<b>To:</b> {to_entity}", body_style))
    if from_entity or to_entity:
        elements.append(Spacer(1, 4*mm))

    # Body lines
    for line in doc_content.get("body_lines", []):
        elements.append(Paragraph(line, body_style))

    if doc_content.get("body_lines"):
        elements.append(Spacer(1, 4*mm))

    # Table (items, line items)
    table_rows = doc_content.get("table_rows", [])
    if table_rows:
        table = Table(table_rows, hAlign="LEFT")
        style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#333333")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
        table.setStyle(style)
        elements.append(table)
        elements.append(Spacer(1, 4*mm))

    # Total
    total = doc_content.get("total_amount", "")
    if total:
        elements.append(Paragraph(f"<b>TOTAL: {total}</b>", body_style))
        elements.append(Spacer(1, 4*mm))

    # Footer
    for line in doc_content.get("footer_lines", []):
        elements.append(Paragraph(line, small_style))

    doc.build(elements)
    return filepath


def generate_fake_document(
    doc_type: str,
    legend: dict,
    output_dir: str,
    context: str = "",
) -> str:
    """Full pipeline: LLM generates content → render to PDF. Returns filepath."""
    content = generate_document_content(doc_type, legend, context)
    filepath = render_pdf(content, output_dir)
    return filepath
