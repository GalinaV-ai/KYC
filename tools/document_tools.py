"""
Document analysis tools.
Uses Claude's vision API to extract and analyze document content.
"""
import base64
import os
import mimetypes
from pathlib import Path
from typing import Optional
import anthropic


def encode_document(file_path: str) -> Optional[dict]:
    """Read and encode a document file for Claude Vision API."""
    path = Path(file_path)
    if not path.exists():
        return None

    mime_type, _ = mimetypes.guess_type(file_path)

    # Supported image types for Claude Vision
    supported_image_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]

    if mime_type in supported_image_types:
        with open(file_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": data
            }
        }
    elif mime_type == "application/pdf":
        # Anthropic API supports native PDF via the "document" content type
        file_size = path.stat().st_size
        max_pdf_size = 30 * 1024 * 1024  # 30 MB limit
        if file_size > max_pdf_size:
            return {
                "type": "text",
                "text": f"[PDF too large: {file_size / 1024 / 1024:.1f} MB. Max supported: 30 MB]"
            }
        with open(file_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": data
            }
        }
    else:
        # Try to read as text
        try:
            text = path.read_text(encoding='utf-8')
            return {"type": "text", "text": text}
        except:
            return {"type": "text", "text": f"[Unsupported file type: {mime_type}]"}


async def analyze_document(file_path: str, doc_type: str, client: anthropic.AsyncAnthropic) -> dict:
    """
    Analyze a document using Claude Vision.

    Args:
        file_path: Path to the document file
        doc_type: Expected document type (passport, utility_bill, certificate_of_incorporation, etc.)
        client: Anthropic client instance

    Returns:
        Dict with extracted data, analysis notes, and any flags
    """
    encoded = encode_document(file_path)
    if not encoded:
        return {"error": f"Could not read file: {file_path}"}

    prompts_by_type = {
        "passport": """Analyze this passport/ID document image. Extract:
1. Full name (as written)
2. Date of birth
3. Nationality
4. Document number
5. Expiry date
6. Issuing country
7. Any visible security features or concerns
8. Does the document look genuine? Any signs of tampering?

Return as JSON with keys: full_name, date_of_birth, nationality, document_number, expiry_date, issuing_country, security_notes, genuineness_assessment""",

        "driving_licence": """Analyze this driving licence image. Extract:
1. Full name
2. Date of birth
3. Address
4. Licence number
5. Expiry date
6. Any concerns about authenticity

Return as JSON with keys: full_name, date_of_birth, address, licence_number, expiry_date, authenticity_notes""",

        "utility_bill": """Analyze this utility bill / proof of address document. Extract:
1. Name on the bill
2. Full address
3. Date of the bill
4. Utility company name
5. Is this bill less than 3 months old?
6. Any concerns

Return as JSON with keys: name, address, bill_date, company, is_recent, concerns""",

        "certificate_of_incorporation": """Analyze this certificate of incorporation. Extract:
1. Company name
2. Company number
3. Date of incorporation
4. Type of company (Ltd, LLP, etc.)
5. Registered office address
6. Any concerns about authenticity

Return as JSON with keys: company_name, company_number, incorporation_date, company_type, registered_address, concerns""",

        "bank_statement": """Analyze this bank statement. Extract:
1. Account holder name
2. Bank name
3. Statement period
4. Opening and closing balance
5. Notable transaction patterns
6. Any concerns

Return as JSON with keys: account_holder, bank_name, period, opening_balance, closing_balance, transaction_notes, concerns""",

        "invoice_sample": """Analyze this invoice. Extract:
1. Invoice number and date
2. Seller/issuer name and address
3. Buyer/recipient name and address
4. Line items (services/products, quantities, prices)
5. Total amount and currency
6. Payment terms
7. Any concerns about authenticity

Return as JSON with keys: invoice_number, date, seller, buyer, line_items, total_amount, currency, payment_terms, concerns""",

        "contract_sample": """Analyze this contract/agreement. Extract:
1. Type of contract (service, supply, employment, etc.)
2. Parties involved (names, roles)
3. Key terms (scope, duration, value)
4. Signatures present?
5. Date signed
6. Any concerns about authenticity

Return as JSON with keys: contract_type, parties, key_terms, has_signatures, date_signed, concerns""",

        "lease_agreement": """Analyze this lease/tenancy agreement. Extract:
1. Property address
2. Landlord name
3. Tenant name
4. Lease period (start/end)
5. Monthly rent amount
6. Any special terms
7. Any concerns

Return as JSON with keys: property_address, landlord, tenant, lease_start, lease_end, monthly_rent, special_terms, concerns""",

        "tax_return": """Analyze this tax return/document. Extract:
1. Taxpayer name
2. Tax period
3. Total income/revenue reported
4. Tax reference number (UTR or similar)
5. Type of return (self-assessment, corporation tax, VAT)
6. Key financial figures
7. Any concerns

Return as JSON with keys: taxpayer_name, tax_period, total_income, tax_reference, return_type, financial_figures, concerns""",

        "business_licence": """Analyze this business licence/permit. Extract:
1. Licence holder name
2. Business name
3. Licence type
4. Issuing authority
5. Issue date and expiry date
6. Licence number
7. Any conditions or restrictions
8. Any concerns about authenticity

Return as JSON with keys: holder_name, business_name, licence_type, issuing_authority, issue_date, expiry_date, licence_number, conditions, concerns""",

        "generic": """Analyze this document. Extract all relevant information including:
1. Document type
2. Key names, dates, addresses
3. Any business-related information
4. Any concerns about authenticity

Return as JSON with keys: document_type, extracted_data, concerns"""
    }

    prompt = prompts_by_type.get(doc_type, prompts_by_type["generic"])

    content = []
    if encoded["type"] in ("image", "document"):
        content.append(encoded)
    elif encoded["type"] == "text":
        # For text-based content (plain text files, fallback), prepend it
        content.append({"type": "text", "text": f"Document content:\n{encoded['text']}"})
    content.append({"type": "text", "text": prompt})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}]
        )

        result_text = response.content[0].text

        # Try to parse as JSON
        import json
        try:
            # Find JSON in the response
            json_start = result_text.find('{')
            json_end = result_text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                extracted = json.loads(result_text[json_start:json_end])
            else:
                extracted = {"raw_analysis": result_text}
        except json.JSONDecodeError:
            extracted = {"raw_analysis": result_text}

        return {
            "doc_type": doc_type,
            "file_path": file_path,
            "extracted_data": extracted,
            "raw_response": result_text,
            "success": True
        }

    except Exception as e:
        return {
            "doc_type": doc_type,
            "file_path": file_path,
            "error": str(e),
            "success": False
        }
