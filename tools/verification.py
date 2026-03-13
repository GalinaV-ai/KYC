"""
Advanced verification tools for business reality checks.
WHOIS, address verification, reviews, VAT, industry benchmarks.
"""
import httpx
import re
import json
from typing import Optional


async def check_domain_whois(domain: str) -> dict:
    """
    Check domain WHOIS data — when was it registered?
    A website registered last week for a business claiming 3 years of trading = red flag.
    Uses free RDAP (Registration Data Access Protocol) — the modern replacement for WHOIS.
    """
    result = {"domain": domain, "found": False}

    # Clean domain
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    async with httpx.AsyncClient() as client:
        try:
            # Try RDAP (free, JSON-based WHOIS replacement)
            resp = await client.get(
                f"https://rdap.org/domain/{domain}",
                timeout=10,
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                result["found"] = True

                # Extract key dates
                for event in data.get("events", []):
                    action = event.get("eventAction", "")
                    date = event.get("eventDate", "")
                    if action == "registration":
                        result["registration_date"] = date
                    elif action == "expiration":
                        result["expiration_date"] = date
                    elif action == "last changed":
                        result["last_updated"] = date

                # Registrar info
                for entity in data.get("entities", []):
                    roles = entity.get("roles", [])
                    if "registrar" in roles:
                        vcard = entity.get("vcardArray", [None, []])[1] if entity.get("vcardArray") else []
                        for item in vcard:
                            if item[0] == "fn":
                                result["registrar"] = item[3]
                                break

                # Name servers
                ns = data.get("nameservers", [])
                result["nameservers"] = [n.get("ldhName", "") for n in ns[:4]]

                # Status
                result["status"] = data.get("status", [])
            else:
                result["error"] = f"RDAP returned {resp.status_code}"
        except Exception as e:
            result["error"] = str(e)

    return result


async def check_address_type(address: str) -> dict:
    """
    Check if a business address is likely a virtual office, residential, or commercial.
    Uses known virtual office providers and patterns.
    """
    result = {"address": address, "type": "unknown", "flags": []}

    address_lower = address.lower()

    # Known virtual office / mail forwarding indicators
    virtual_office_keywords = [
        "regus", "wework", "spaces", "hq ", "virtual office",
        "mail box", "mailbox", "po box", "p.o. box",
        "registered office only", "c/o ", "suite", "office 1",
        "serviced office", "business centre", "innovation centre",
        "the old", "barn", "cottage",  # residential sounding for a business
    ]

    # Known virtual office addresses (major UK ones)
    known_virtual = [
        "20-22 wenlock road", "86-90 paul street", "71-75 shelton street",
        "167-169 great portland street", "2nd floor college house",
        "128 city road", "7 bell yard", "3rd floor", "kemp house",
        "27 old gloucester street", "63-66 hatton garden",
        "145-157 st john street",
    ]

    for kw in virtual_office_keywords:
        if kw in address_lower:
            result["type"] = "likely_virtual_office"
            result["flags"].append(f"Contains virtual office keyword: '{kw}'")

    for addr in known_virtual:
        if addr in address_lower:
            result["type"] = "known_virtual_office"
            result["flags"].append(f"Known virtual office address: '{addr}'")

    # Check postcode areas known for virtual offices
    # EC1, EC2, WC1, WC2, W1 are common
    postcode_match = re.search(r'([A-Z]{1,2}\d{1,2})\s', address.upper())
    if postcode_match:
        result["postcode_area"] = postcode_match.group(1)

    # If no flags, try a web search for the address
    if not result["flags"]:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": f'"{address}" virtual office OR "serviced office" OR "registered office"',
                            "format": "json", "no_html": 1},
                    timeout=8,
                    follow_redirects=True
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("Abstract") or data.get("RelatedTopics"):
                        result["type"] = "possibly_virtual"
                        result["flags"].append("Web search suggests this may be a serviced/virtual office address")
                    else:
                        result["type"] = "no_flags"
            except:
                pass

    return result


async def search_reviews(business_name: str, location: str = "UK") -> dict:
    """
    Search for business reviews on Trustpilot, Google, etc.
    A real business usually has SOME online reviews, even if few.
    """
    result = {"business_name": business_name, "sources_checked": [], "reviews_found": False}

    queries = [
        f"{business_name} reviews trustpilot",
        f"{business_name} {location} reviews",
        f'"{business_name}" review OR feedback OR rating',
    ]

    async with httpx.AsyncClient() as client:
        for query in queries:
            try:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1},
                    timeout=8,
                    follow_redirects=True
                )
                if resp.status_code == 200:
                    data = resp.json()
                    source_result = {"query": query, "found": False, "details": ""}

                    if data.get("Abstract"):
                        source_result["found"] = True
                        source_result["details"] = data["Abstract"][:300]
                        result["reviews_found"] = True

                    for topic in data.get("RelatedTopics", [])[:3]:
                        if isinstance(topic, dict) and "Text" in topic:
                            text = topic["Text"].lower()
                            if any(kw in text for kw in ["review", "rating", "star", "trustpilot", "feedback"]):
                                source_result["found"] = True
                                source_result["details"] += " | " + topic["Text"][:200]
                                result["reviews_found"] = True

                    result["sources_checked"].append(source_result)
            except Exception as e:
                result["sources_checked"].append({"query": query, "error": str(e)})

    return result


async def verify_vat_number(vat_number: str) -> dict:
    """
    Verify a UK VAT number using the EU VIES service or HMRC API.
    A registered VAT number confirms the business has revenue above the VAT threshold (£85k).
    """
    result = {"vat_number": vat_number, "valid": False}

    # Clean the VAT number
    vat_clean = re.sub(r'[^0-9]', '', vat_number.replace("GB", ""))

    if len(vat_clean) != 9:
        result["error"] = f"UK VAT numbers should be 9 digits, got {len(vat_clean)}"
        return result

    # Try HMRC VAT check API (free, no key needed for basic check)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"https://api.service.hmrc.gov.uk/organisations/vat/check-vat-number/lookup/{vat_clean}",
                timeout=10,
                headers={"Accept": "application/json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                target = data.get("target", {})
                result["valid"] = True
                result["name"] = target.get("name", "")
                result["address"] = target.get("address", {})
                result["consultation_number"] = data.get("consultationNumber", "")
            elif resp.status_code == 404:
                result["valid"] = False
                result["note"] = "VAT number not found in HMRC records"
            else:
                result["error"] = f"HMRC API returned {resp.status_code}"
        except Exception as e:
            # Fallback: try VIES (EU service, works for GB numbers too sometimes)
            try:
                resp = await client.get(
                    f"https://ec.europa.eu/taxation_customs/vies/rest-api/ms/GB/vat/{vat_clean}",
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["valid"] = data.get("isValid", False)
                    result["name"] = data.get("name", "")
                    result["source"] = "VIES"
                else:
                    result["error"] = f"Both HMRC and VIES failed: {str(e)}"
            except Exception as e2:
                result["error"] = f"VAT verification unavailable: {str(e2)}"

    return result


async def get_industry_benchmarks(industry: str, business_type: str = "") -> dict:
    """
    Return typical industry benchmarks for UK small businesses.
    Used to sanity-check claimed revenue, margins, and employee counts.
    These are hardcoded reference data — in production, pull from ONS/HMRC statistics.
    """
    benchmarks = {
        "restaurant": {
            "avg_monthly_turnover_gbp": "15000-60000",
            "typical_margin_pct": "3-9",
            "avg_employees": "5-20",
            "typical_costs": ["food (28-35%)", "labour (25-35%)", "rent (5-10%)", "utilities"],
            "typical_customers_per_day": "50-200",
            "avg_spend_per_head_gbp": "12-35",
            "vat_registered_threshold": "Usually yes (threshold £85k)",
            "cash_ratio": "Mixed — card payments dominate, some cash",
            "notes": "Margins are thin. High failure rate in first 2 years."
        },
        "construction": {
            "avg_monthly_turnover_gbp": "20000-150000",
            "typical_margin_pct": "5-15",
            "avg_employees": "3-30",
            "typical_costs": ["materials (40-60%)", "labour/subcontractors (20-40%)", "insurance", "vehicle"],
            "required_insurance": ["public liability", "employers liability", "professional indemnity"],
            "vat_registered_threshold": "Usually yes",
            "cash_ratio": "Mostly bank transfer / invoice. Cash for small jobs.",
            "notes": "CIS (Construction Industry Scheme) applies. Sub-contractor tax deductions."
        },
        "cleaning": {
            "avg_monthly_turnover_gbp": "3000-25000",
            "typical_margin_pct": "10-25",
            "avg_employees": "2-15",
            "typical_costs": ["labour (50-65%)", "supplies (5-10%)", "transport", "insurance"],
            "vat_registered_threshold": "Sometimes below threshold",
            "cash_ratio": "Mixed — commercial clients by invoice, domestic can be cash",
            "notes": "Low barrier to entry. Many sole traders. Domestic vs commercial matters a lot."
        },
        "ecommerce": {
            "avg_monthly_turnover_gbp": "5000-80000",
            "typical_margin_pct": "15-40 (depends on product)",
            "avg_employees": "1-10",
            "typical_costs": ["product/stock (30-60%)", "shipping (5-15%)", "platform fees (5-15%)", "advertising (10-20%)"],
            "typical_platforms": ["Amazon", "Shopify", "eBay", "Etsy"],
            "vat_registered_threshold": "Varies — online sellers often cross threshold quickly",
            "cash_ratio": "Zero cash — all digital payments",
            "notes": "Returns rate 15-30%. Ad spend is significant cost. Stock management crucial."
        },
        "consulting": {
            "avg_monthly_turnover_gbp": "5000-40000",
            "typical_margin_pct": "40-70",
            "avg_employees": "1-5",
            "typical_costs": ["own time (primary)", "professional insurance", "software/tools", "travel"],
            "typical_day_rate_gbp": "300-1500",
            "vat_registered_threshold": "Usually yes for established consultants",
            "cash_ratio": "Zero cash — bank transfer / invoice only",
            "notes": "High margins but revenue depends on utilization rate. Typical 60-80% billable."
        },
        "retail_shop": {
            "avg_monthly_turnover_gbp": "8000-50000",
            "typical_margin_pct": "20-50 (varies by product)",
            "avg_employees": "2-8",
            "typical_costs": ["stock (40-60%)", "rent (10-15%)", "staff (15-25%)", "utilities"],
            "vat_registered_threshold": "Usually yes",
            "cash_ratio": "Mixed — trend toward card but some cash",
            "notes": "Location is critical cost factor. Seasonal variation common."
        },
        "beauty_salon": {
            "avg_monthly_turnover_gbp": "5000-25000",
            "typical_margin_pct": "15-30",
            "avg_employees": "2-8",
            "typical_costs": ["rent (15-25%)", "products (10-20%)", "staff (30-45%)"],
            "vat_registered_threshold": "Some below, some above",
            "cash_ratio": "Decreasing cash — mostly card/online booking",
            "notes": "Chair rental model common. Booking platforms important."
        },
        "transport_logistics": {
            "avg_monthly_turnover_gbp": "5000-60000",
            "typical_margin_pct": "5-15",
            "avg_employees": "1-20",
            "typical_costs": ["fuel (25-35%)", "vehicle (lease/maintenance 20-30%)", "insurance (10-15%)", "driver costs"],
            "required_insurance": ["goods in transit", "public liability", "fleet insurance"],
            "vat_registered_threshold": "Usually yes",
            "cash_ratio": "Mostly invoice/bank transfer",
            "notes": "Operator's licence required for goods over 3.5t. High fuel cost sensitivity."
        },
        "food_takeaway": {
            "avg_monthly_turnover_gbp": "8000-40000",
            "typical_margin_pct": "5-15",
            "avg_employees": "3-12",
            "typical_costs": ["food (28-35%)", "delivery platform fees (15-30%)", "staff (20-30%)", "rent"],
            "typical_platforms": ["Deliveroo", "Uber Eats", "Just Eat"],
            "vat_registered_threshold": "Often yes",
            "cash_ratio": "Mostly digital via platforms. Walk-in can be cash.",
            "notes": "Platform commissions eat margins significantly. Food hygiene rating essential."
        },
        "it_services": {
            "avg_monthly_turnover_gbp": "5000-50000",
            "typical_margin_pct": "30-60",
            "avg_employees": "1-10",
            "typical_costs": ["staff/contractors (40-60%)", "software licences", "hosting", "insurance"],
            "vat_registered_threshold": "Usually yes",
            "cash_ratio": "Zero cash — all invoiced",
            "notes": "Project-based or retainer-based. IR35 implications for contractors."
        },
        "default": {
            "note": "No specific benchmarks for this industry. Use general UK SME data.",
            "avg_uk_sme_turnover_gbp": "10000-50000 per month for small business",
            "avg_uk_sme_employees": "1-10",
            "vat_threshold_gbp": "85000 annual (as of 2024/25)",
            "corporation_tax": "19-25% depending on profits",
        }
    }

    # Try to match industry
    industry_lower = industry.lower()
    matched = None
    for key in benchmarks:
        if key in industry_lower or industry_lower in key:
            matched = key
            break

    # Fuzzy matching
    if not matched:
        keyword_map = {
            "restaurant": ["food", "cafe", "catering", "dining", "eat"],
            "construction": ["build", "plumb", "electric", "renovate", "trade"],
            "cleaning": ["clean", "janitorial", "housekeep"],
            "ecommerce": ["online", "shop", "amazon", "shopify", "ebay", "etsy"],
            "consulting": ["consult", "advisory", "management consult", "freelance"],
            "retail_shop": ["retail", "shop", "store", "boutique"],
            "beauty_salon": ["beauty", "salon", "hair", "nail", "barber", "spa"],
            "transport_logistics": ["transport", "logistics", "delivery", "courier", "haulage"],
            "food_takeaway": ["takeaway", "take away", "delivery food", "fast food", "kebab", "pizza"],
            "it_services": ["it ", "software", "web develop", "app develop", "tech", "digital"],
        }
        for key, keywords in keyword_map.items():
            if any(kw in industry_lower for kw in keywords):
                matched = key
                break

    data = benchmarks.get(matched or "default", benchmarks["default"])
    return {
        "industry_matched": matched or "default",
        "query": industry,
        "benchmarks": data
    }


async def search_google_maps(query: str) -> dict:
    """
    Search for a business on Google Maps via web search.
    A real local business usually appears on Google Maps with reviews.
    In production, use Google Places API.
    """
    result = {"query": query, "found": False}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": f"{query} site:google.com/maps OR site:google.co.uk/maps",
                        "format": "json", "no_html": 1},
                timeout=8,
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("Abstract") or data.get("RelatedTopics"):
                    result["found"] = True
                    result["details"] = data.get("Abstract", "")[:300]
                    for topic in data.get("RelatedTopics", [])[:3]:
                        if isinstance(topic, dict):
                            result.setdefault("related", []).append(topic.get("Text", "")[:200])
        except Exception as e:
            result["error"] = str(e)

    return result
