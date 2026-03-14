"""
UK Government register checks for KYC verification.
FCA, ICO, Charity Commission, Disqualified Directors, London Gazette,
Insolvency Register, Food Hygiene (FSA), CQC, Gambling Commission,
HMRC MSB Register, and industry-specific professional bodies.
All free APIs or web-search fallbacks.
"""
import httpx
import re
import json
from typing import Optional
from datetime import datetime

try:
    from tools.web_search import _search_ddg
except ImportError:
    from web_search import _search_ddg


# ─────────────────────────────────────────────
# A1. FCA Register (Financial Conduct Authority)
# ─────────────────────────────────────────────

async def check_fca_register(firm_name: str, firm_number: str = "") -> dict:
    """
    Check if a firm is registered with the FCA.
    Also checks the FCA Warning List for known scam firms.
    Free API: https://register.fca.org.uk/
    """
    result = {
        "source": "fca_register",
        "firm_name": firm_name,
        "registered": False,
        "warning_list": False,
        "details": {}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        # Search FCA register
        try:
            resp = await client.get(
                "https://register.fca.org.uk/services/V0.1/Search",
                params={"q": firm_number or firm_name, "type": "firm"},
                headers={"Accept": "application/json"},
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("Data", [])
                if results:
                    result["registered"] = True
                    first = results[0] if isinstance(results, list) else results
                    result["details"] = {
                        "name": first.get("Name", ""),
                        "frn": first.get("FRN", ""),
                        "status": first.get("Status", ""),
                        "type": first.get("Type", ""),
                    }
                    # Multiple matches
                    if isinstance(results, list) and len(results) > 1:
                        result["details"]["total_matches"] = len(results)
        except Exception as e:
            result["fca_error"] = str(e)

        # Check FCA Warning List via web search
        try:
            warnings = await _search_ddg(
                f'site:fca.org.uk/news "{firm_name}" warning OR scam OR unauthorised',
                max_results=3
            )
            for w in warnings:
                if not isinstance(w, dict) or "error" in w:
                    continue
                title = w.get("title", "").lower()
                if any(kw in title for kw in ["warning", "scam", "unauthorised", "clone"]):
                    result["warning_list"] = True
                    result["warning_details"] = w.get("title", "") + " — " + w.get("href", "")
                    break
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# A2. ICO Register (Data Protection)
# ─────────────────────────────────────────────

async def check_ico_register(organisation_name: str) -> dict:
    """
    Check if an organisation is registered with the ICO (Information Commissioner's Office).
    Any UK business processing personal data should be registered.
    Registration costs £40-60/year — absence is a signal of informality.
    """
    result = {
        "source": "ico_register",
        "organisation": organisation_name,
        "registered": False,
        "details": {}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://ico.org.uk/ESDWebPages/Search",
                params={"q": organisation_name},
                follow_redirects=True
            )
            if resp.status_code == 200:
                html = resp.text.lower()
                # Parse results — ICO returns HTML, we check for registration numbers
                # ICO reg numbers follow pattern: ZA######, Z#######, etc.
                reg_numbers = re.findall(r'z[a-z]?\d{6,7}', html)
                if reg_numbers:
                    result["registered"] = True
                    result["details"]["registration_numbers"] = list(set(reg_numbers[:5]))

                # Check if org name appears in results
                if organisation_name.lower() in html:
                    result["details"]["name_found_in_results"] = True
        except Exception as e:
            result["error"] = str(e)

        # Fallback: web search
        if not result["registered"]:
            try:
                search_results = await _search_ddg(
                    f'site:ico.org.uk "{organisation_name}"',
                    max_results=3
                )
                for sr in search_results:
                    if isinstance(sr, dict) and "href" in sr:
                        if "ico.org.uk" in sr.get("href", ""):
                            result["details"]["found_via_search"] = True
                            result["details"]["search_url"] = sr["href"]
                            break
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────
# A3. Charity Commission
# ─────────────────────────────────────────────

async def check_charity_commission(name: str, charity_number: str = "") -> dict:
    """
    Check if an organisation is a registered charity.
    Free API: https://api.charitycommission.gov.uk/
    """
    result = {
        "source": "charity_commission",
        "name": name,
        "registered": False,
        "details": {}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # Charity Commission API (Beta)
            search_url = "https://api.charitycommission.gov.uk/register/api/allcharitydetailsV2/0/"
            if charity_number:
                search_url = f"https://api.charitycommission.gov.uk/register/api/allcharitydetailsV2/0/{charity_number}/0"

            # Alternative: use the search endpoint
            resp = await client.get(
                "https://api.charitycommission.gov.uk/register/api/searchCharities",
                params={"searchText": charity_number or name, "pageNumber": 1, "pageSize": 5},
                headers={"Accept": "application/json"},
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                charities = data if isinstance(data, list) else data.get("charities", [])
                if charities:
                    result["registered"] = True
                    first = charities[0]
                    result["details"] = {
                        "charity_number": first.get("registeredCharityNumber", ""),
                        "name": first.get("charityName", ""),
                        "status": first.get("registrationStatus", ""),
                        "date_registered": first.get("dateOfRegistration", ""),
                        "income": first.get("latestIncome", ""),
                    }
        except Exception as e:
            result["error"] = str(e)

        # Fallback: web search
        if not result["registered"]:
            try:
                results = await _search_ddg(
                    f'site:register-of-charities.charitycommission.gov.uk "{name}"',
                    max_results=3
                )
                for r in results:
                    if isinstance(r, dict) and "charitycommission" in r.get("href", ""):
                        result["details"]["found_via_search"] = True
                        result["details"]["url"] = r["href"]
                        break
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────
# A4. Disqualified Directors Register
# ─────────────────────────────────────────────

async def check_disqualified_directors(person_name: str) -> dict:
    """
    Check if a person appears on the Companies House disqualified directors register.
    Disqualified directors cannot act as company directors.
    Free API via Companies House.
    """
    import os
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "source": "disqualified_directors",
        "person_name": person_name,
        "disqualified": False,
        "details": {}
    }

    if api_key:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(
                    "https://api.company-information.service.gov.uk/search/disqualified-officers",
                    params={"q": person_name, "items_per_page": 5},
                    auth=(api_key, ""),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    if items:
                        result["disqualified"] = True
                        result["details"]["matches"] = []
                        for item in items[:3]:
                            result["details"]["matches"].append({
                                "name": item.get("title", ""),
                                "date_of_birth": item.get("date_of_birth", {}),
                                "disqualifications": item.get("disqualifications", []),
                            })
            except Exception as e:
                result["error"] = str(e)
    else:
        # Fallback: web search
        try:
            results = await _search_ddg(
                f'"{person_name}" disqualified director companies house',
                max_results=3
            )
            for r in results:
                if isinstance(r, dict) and "body" in r:
                    body = r.get("body", "").lower()
                    if "disqualif" in body:
                        result["details"]["found_via_search"] = True
                        result["details"]["snippet"] = r["body"][:300]
                        break
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# A5. London Gazette (Insolvency Notices)
# ─────────────────────────────────────────────

async def check_london_gazette(name: str, is_company: bool = True) -> dict:
    """
    Search the London Gazette for insolvency notices, winding-up petitions,
    company dissolutions, and other legal notices.
    Free search at thegazette.co.uk.
    """
    result = {
        "source": "london_gazette",
        "name": name,
        "notices_found": False,
        "notices": []
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # The Gazette has a search API
            resp = await client.get(
                "https://www.thegazette.co.uk/notice/search",
                params={
                    "text": f'"{name}"',
                    "categorycode": "all",
                    "results-page-size": 10,
                },
                headers={"Accept": "application/json"},
                follow_redirects=True
            )
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "json" in content_type:
                    data = resp.json()
                    entries = data.get("results", data.get("notices", []))
                    if entries:
                        result["notices_found"] = True
                        for entry in entries[:5]:
                            result["notices"].append({
                                "title": entry.get("title", ""),
                                "category": entry.get("category", ""),
                                "date": entry.get("date", ""),
                                "url": entry.get("url", ""),
                            })
                else:
                    # HTML response — parse for key indicators
                    html = resp.text.lower()
                    danger_keywords = [
                        "winding-up", "winding up", "liquidation", "dissolution",
                        "bankruptcy", "insolvency", "struck off", "compulsory",
                        "administration order", "voluntary arrangement"
                    ]
                    for kw in danger_keywords:
                        if kw in html:
                            result["notices_found"] = True
                            result["notices"].append({
                                "type": kw,
                                "note": f"'{kw}' found in Gazette search results"
                            })
        except Exception as e:
            result["error"] = str(e)

        # Always supplement with web search for broader coverage
        try:
            gazette_search = await _search_ddg(
                f'site:thegazette.co.uk "{name}" insolvency OR winding OR dissolution OR bankruptcy',
                max_results=3
            )
            for sr in gazette_search:
                if isinstance(sr, dict) and "thegazette" in sr.get("href", ""):
                    result["notices_found"] = True
                    result["notices"].append({
                        "source": "web_search",
                        "title": sr.get("title", ""),
                        "url": sr.get("href", ""),
                        "snippet": sr.get("body", "")[:200]
                    })
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# A6. Insolvency Register (Individual)
# ─────────────────────────────────────────────

async def check_insolvency_register(person_name: str) -> dict:
    """
    Check the Individual Insolvency Register for bankruptcy, IVAs, DROs.
    Free search at insolvencydirect.bis.gov.uk.
    """
    result = {
        "source": "insolvency_register",
        "person_name": person_name,
        "found": False,
        "details": {}
    }

    # The Insolvency Service doesn't have a clean REST API,
    # so we use web search as primary method
    try:
        results = await _search_ddg(
            f'"{person_name}" site:insolvencydirect.bis.gov.uk OR '
            f'"{person_name}" bankruptcy OR IVA OR "debt relief order"',
            max_results=5
        )
        for r in results:
            if not isinstance(r, dict) or "error" in r:
                continue
            body = r.get("body", "").lower()
            href = r.get("href", "").lower()
            if "insolvencydirect" in href or any(
                kw in body for kw in ["bankrupt", "iva", "debt relief", "insolvency"]
            ):
                name_parts = person_name.lower().split()
                if any(part in body for part in name_parts if len(part) > 2):
                    result["found"] = True
                    result["details"]["source"] = r.get("href", "")
                    result["details"]["snippet"] = r.get("body", "")[:300]
                    break
    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# A7. Food Hygiene Ratings (FSA)
# ─────────────────────────────────────────────

async def check_food_hygiene_rating(business_name: str, location: str = "") -> dict:
    """
    Check food hygiene ratings from the Food Standards Agency.
    Free API: https://api.ratings.food.gov.uk/
    Essential for restaurants, takeaways, food manufacturers.
    """
    result = {
        "source": "food_hygiene_fsa",
        "business_name": business_name,
        "found": False,
        "details": {}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            params = {"name": business_name, "pageSize": 5}
            if location:
                params["address"] = location

            resp = await client.get(
                "https://api.ratings.food.gov.uk/Establishments",
                params=params,
                headers={
                    "x-api-version": "2",
                    "Accept": "application/json"
                },
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                establishments = data.get("establishments", [])
                if establishments:
                    result["found"] = True
                    result["details"]["total_matches"] = len(establishments)
                    result["details"]["establishments"] = []
                    for est in establishments[:3]:
                        result["details"]["establishments"].append({
                            "name": est.get("BusinessName", ""),
                            "type": est.get("BusinessType", ""),
                            "rating": est.get("RatingValue", ""),
                            "rating_date": est.get("RatingDate", ""),
                            "address": {
                                "line1": est.get("AddressLine1", ""),
                                "line2": est.get("AddressLine2", ""),
                                "city": est.get("AddressLine3", ""),
                                "postcode": est.get("PostCode", ""),
                            },
                            "local_authority": est.get("LocalAuthorityName", ""),
                        })
        except Exception as e:
            result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# A8. CQC Register (Care Quality Commission)
# ─────────────────────────────────────────────

async def check_cqc_register(provider_name: str) -> dict:
    """
    Check CQC registration for healthcare/care providers.
    Free API: https://api.cqc.org.uk/public/v1/
    """
    result = {
        "source": "cqc_register",
        "provider_name": provider_name,
        "registered": False,
        "details": {}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://api.cqc.org.uk/public/v1/providers",
                params={"partnerCode": "", "q": provider_name, "page": 1, "perPage": 5},
                headers={"Accept": "application/json"},
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                providers = data.get("providers", [])
                if providers:
                    result["registered"] = True
                    result["details"]["total_matches"] = data.get("total", 0)
                    result["details"]["providers"] = []
                    for p in providers[:3]:
                        provider_id = p.get("providerId", "")
                        prov_detail = {"name": p.get("providerName", ""), "id": provider_id}

                        # Get detailed info if we have an ID
                        if provider_id:
                            try:
                                detail_resp = await client.get(
                                    f"https://api.cqc.org.uk/public/v1/providers/{provider_id}",
                                    headers={"Accept": "application/json"}
                                )
                                if detail_resp.status_code == 200:
                                    detail = detail_resp.json()
                                    prov_detail["type"] = detail.get("type", "")
                                    prov_detail["overall_rating"] = detail.get("currentRatings", {}).get("overall", {}).get("rating", "")
                                    prov_detail["inspection_date"] = detail.get("currentRatings", {}).get("overall", {}).get("reportDate", "")
                            except Exception:
                                pass

                        result["details"]["providers"].append(prov_detail)
        except Exception as e:
            result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# A9 + A10. Companies House Extended
# (Charges register + Overdue filing check)
# ─────────────────────────────────────────────

async def check_company_charges(company_number: str) -> dict:
    """
    Check the charges register (secured loans/mortgages on the company).
    Multiple outstanding charges may indicate financial stress.
    """
    import os
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "source": "companies_house_charges",
        "company_number": company_number,
        "has_charges": False,
        "details": {}
    }

    if not api_key:
        result["error"] = "COMPANIES_HOUSE_API_KEY not set"
        return result

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"https://api.company-information.service.gov.uk/company/{company_number}/charges",
                auth=(api_key, ""),
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                result["has_charges"] = len(items) > 0
                result["details"]["total_count"] = data.get("total_count", 0)
                result["details"]["unfiltered_count"] = data.get("unfiltered_count", 0)
                result["details"]["charges"] = []
                for charge in items[:5]:
                    result["details"]["charges"].append({
                        "status": charge.get("status", ""),
                        "created_on": charge.get("created_on", ""),
                        "delivered_on": charge.get("delivered_on", ""),
                        "classification": charge.get("classification", {}).get("description", ""),
                        "persons_entitled": [p.get("name", "") for p in charge.get("persons_entitled", [])],
                        "secured_details": charge.get("particulars", {}).get("description", ""),
                    })
            elif resp.status_code == 404:
                result["details"]["note"] = "No charges registered"
        except Exception as e:
            result["error"] = str(e)

    return result


async def check_filing_compliance(company_number: str) -> dict:
    """
    Check if the company has overdue filings (confirmation statement, accounts).
    Overdue filings = risk of being struck off.
    """
    import os
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "source": "filing_compliance",
        "company_number": company_number,
        "compliant": True,
        "overdue": [],
        "details": {}
    }

    if not api_key:
        result["error"] = "COMPANIES_HOUSE_API_KEY not set"
        return result

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # Get company profile for filing dates
            resp = await client.get(
                f"https://api.company-information.service.gov.uk/company/{company_number}",
                auth=(api_key, ""),
            )
            if resp.status_code == 200:
                data = resp.json()
                result["details"]["company_name"] = data.get("company_name", "")
                result["details"]["company_status"] = data.get("company_status", "")

                # Check confirmation statement
                cs = data.get("confirmation_statement", {})
                if cs:
                    next_due = cs.get("next_due", "")
                    last_made = cs.get("last_made_up_to", "")
                    result["details"]["confirmation_statement"] = {
                        "next_due": next_due,
                        "last_made": last_made,
                        "overdue": cs.get("overdue", False)
                    }
                    if cs.get("overdue"):
                        result["compliant"] = False
                        result["overdue"].append("confirmation_statement")

                # Check accounts
                accounts = data.get("accounts", {})
                if accounts:
                    next_due = accounts.get("next_due", "")
                    last_made = accounts.get("last_accounts", {}).get("made_up_to", "")
                    result["details"]["accounts"] = {
                        "next_due": next_due,
                        "last_made": last_made,
                        "overdue": accounts.get("overdue", False),
                        "type": accounts.get("last_accounts", {}).get("type", ""),
                    }
                    if accounts.get("overdue"):
                        result["compliant"] = False
                        result["overdue"].append("accounts")

                # Check if company is at risk
                if data.get("has_been_liquidated"):
                    result["details"]["liquidated"] = True
                    result["compliant"] = False
                    result["overdue"].append("company_liquidated")

                if data.get("has_insolvency_history"):
                    result["details"]["insolvency_history"] = True

        except Exception as e:
            result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# F1. Gambling Commission
# ─────────────────────────────────────────────

async def check_gambling_commission(operator_name: str) -> dict:
    """
    Check the Gambling Commission public register.
    Required for any gambling/betting business in the UK.
    """
    result = {
        "source": "gambling_commission",
        "operator_name": operator_name,
        "licensed": False,
        "details": {}
    }

    try:
        results = await _search_ddg(
            f'site:gamblingcommission.gov.uk "{operator_name}"',
            max_results=3
        )
        for r in results:
            if isinstance(r, dict) and "gamblingcommission" in r.get("href", ""):
                result["licensed"] = True
                result["details"]["url"] = r["href"]
                result["details"]["snippet"] = r.get("body", "")[:300]
                break
    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# F2. HMRC Money Service Business Register
# ─────────────────────────────────────────────

async def check_hmrc_msb_register(business_name: str) -> dict:
    """
    Check if a business is registered as a Money Service Business with HMRC.
    Required for money transfer, currency exchange, cheque cashing businesses.
    """
    result = {
        "source": "hmrc_msb_register",
        "business_name": business_name,
        "registered": False,
        "details": {}
    }

    try:
        results = await _search_ddg(
            f'"{business_name}" HMRC "money service" OR "money laundering regulations" registered',
            max_results=5
        )
        for r in results:
            if not isinstance(r, dict) or "error" in r:
                continue
            body = r.get("body", "").lower()
            if "money service" in body or "msr" in body or "hmrc" in body:
                name_lower = business_name.lower()
                if any(part in body for part in name_lower.split() if len(part) > 3):
                    result["registered"] = True
                    result["details"]["snippet"] = r.get("body", "")[:300]
                    result["details"]["url"] = r.get("href", "")
                    break
    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# F3-F6. Professional Body Checks (generic)
# ─────────────────────────────────────────────

PROFESSIONAL_BODIES = {
    "solicitor": {
        "body": "SRA (Solicitors Regulation Authority)",
        "site": "sra.org.uk",
        "query_template": 'site:sra.org.uk "{name}"',
    },
    "accountant": {
        "body": "ICAEW / ACCA",
        "site": "icaew.com OR acca.org",
        "query_template": '"{name}" site:icaew.com OR site:accaglobal.com',
    },
    "doctor": {
        "body": "GMC (General Medical Council)",
        "site": "gmc-uk.org",
        "query_template": 'site:gmc-uk.org "{name}"',
    },
    "dentist": {
        "body": "GDC (General Dental Council)",
        "site": "gdc-uk.org",
        "query_template": 'site:gdc-uk.org "{name}"',
    },
    "architect": {
        "body": "ARB (Architects Registration Board)",
        "site": "arb.org.uk",
        "query_template": 'site:arb.org.uk "{name}"',
    },
    "estate_agent": {
        "body": "The Property Ombudsman / NAEA Propertymark",
        "site": "tpos.co.uk OR propertymark.co.uk",
        "query_template": '"{name}" site:tpos.co.uk OR site:propertymark.co.uk',
    },
    "security": {
        "body": "SIA (Security Industry Authority)",
        "site": "sia.homeoffice.gov.uk",
        "query_template": '"{name}" SIA licence OR "security industry authority"',
    },
    "construction": {
        "body": "CITB / CSCS / FMB",
        "site": "citb.co.uk OR cscs.uk.com OR fmb.org.uk",
        "query_template": '"{name}" site:citb.co.uk OR site:fmb.org.uk OR CSCS',
    },
    "financial_adviser": {
        "body": "FCA (Financial Conduct Authority)",
        "site": "register.fca.org.uk",
        "query_template": 'site:register.fca.org.uk "{name}"',
    },
    "nurse": {
        "body": "NMC (Nursing and Midwifery Council)",
        "site": "nmc.org.uk",
        "query_template": 'site:nmc.org.uk "{name}"',
    },
    "teacher": {
        "body": "TRA (Teaching Regulation Agency)",
        "site": "teacherservices.education.gov.uk",
        "query_template": '"{name}" "teaching regulation" OR "prohibited from teaching"',
    },
    "transport": {
        "body": "Traffic Commissioner / DVSA",
        "site": "gov.uk",
        "query_template": '"{name}" "operator licence" OR "goods vehicle" site:gov.uk',
    },
}


async def check_professional_registration(
    name: str,
    profession: str,
) -> dict:
    """
    Check professional body registration for a given profession.
    Covers: solicitors, accountants, doctors, dentists, architects,
    estate agents, security, construction, financial advisers, nurses, teachers, transport.
    """
    result = {
        "source": "professional_body",
        "name": name,
        "profession": profession,
        "registered": False,
        "details": {}
    }

    # Find matching profession
    profession_lower = profession.lower()
    matched_key = None
    for key in PROFESSIONAL_BODIES:
        if key in profession_lower or profession_lower in key:
            matched_key = key
            break

    # Fuzzy match
    if not matched_key:
        fuzzy_map = {
            "solicitor": ["law", "legal", "lawyer", "barrister"],
            "accountant": ["accounting", "audit", "bookkeep", "chartered"],
            "doctor": ["medical", "physician", "gp", "surgery", "clinic"],
            "dentist": ["dental", "orthodont"],
            "architect": ["architecture", "building design"],
            "estate_agent": ["property", "estate", "letting", "real estate"],
            "security": ["security", "guarding", "bouncer", "door supervisor"],
            "construction": ["builder", "plumber", "electrician", "contractor"],
            "financial_adviser": ["financial advi", "wealth manage", "investment"],
            "nurse": ["nursing", "midwife", "healthcare assist"],
            "transport": ["haulage", "logistics", "freight", "taxi", "private hire"],
        }
        for key, keywords in fuzzy_map.items():
            if any(kw in profession_lower for kw in keywords):
                matched_key = key
                break

    if not matched_key:
        result["details"]["note"] = f"No professional body mapping for '{profession}'"
        return result

    body_info = PROFESSIONAL_BODIES[matched_key]
    result["details"]["professional_body"] = body_info["body"]

    try:
        query = body_info["query_template"].format(name=name)
        results = await _search_ddg(query, max_results=5)
        for r in results:
            if not isinstance(r, dict) or "error" in r:
                continue
            body_text = r.get("body", "").lower()
            name_parts = name.lower().split()
            if any(part in body_text for part in name_parts if len(part) > 2):
                result["registered"] = True
                result["details"]["url"] = r.get("href", "")
                result["details"]["snippet"] = r.get("body", "")[:300]
                break
    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# B2. Adverse Media Screening
# ─────────────────────────────────────────────

async def check_adverse_media(name: str, company_name: str = "") -> dict:
    """
    Targeted adverse media screening.
    Searches for: fraud, money laundering, court cases, regulatory action,
    criminal proceedings, sanctions evasion, tax evasion.
    More targeted than general news search.
    """
    result = {
        "source": "adverse_media",
        "name": name,
        "company_name": company_name,
        "adverse_found": False,
        "hits": []
    }

    adverse_keywords = [
        "fraud", "money laundering", "criminal", "court case",
        "prosecuted", "convicted", "fined", "regulatory action",
        "banned", "disqualified", "sanctions", "tax evasion",
        "scam", "arrest", "charged", "investigation",
        "seized", "confiscated", "tribunal", "misconduct",
    ]

    # Build search queries
    queries = []
    kw_groups = [
        "fraud OR \"money laundering\" OR criminal OR convicted",
        "scam OR prosecuted OR fined OR arrested",
        "sanctions OR \"tax evasion\" OR seized OR banned",
        "court OR tribunal OR investigation OR misconduct",
    ]

    for kw_group in kw_groups:
        queries.append(f'"{name}" {kw_group}')
        if company_name:
            queries.append(f'"{company_name}" {kw_group}')

    seen_urls = set()
    for query in queries:
        try:
            results = await _search_ddg(query, max_results=3)
            for r in results:
                if not isinstance(r, dict) or "error" in r:
                    continue
                url = r.get("href", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                body = r.get("body", "").lower()
                title = r.get("title", "").lower()
                combined = body + " " + title

                # Check if the name actually appears in the result
                name_parts = name.lower().split()
                name_match = any(part in combined for part in name_parts if len(part) > 2)
                if not name_match and company_name:
                    company_parts = company_name.lower().split()
                    name_match = any(part in combined for part in company_parts if len(part) > 3)

                if not name_match:
                    continue

                # Check for adverse keywords
                matched_keywords = [kw for kw in adverse_keywords if kw in combined]
                if matched_keywords:
                    result["adverse_found"] = True
                    result["hits"].append({
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": r.get("body", "")[:300],
                        "matched_keywords": matched_keywords,
                    })
        except Exception:
            continue

    # Deduplicate by URL
    result["hits"] = result["hits"][:10]  # Cap at 10 hits
    return result


# ─────────────────────────────────────────────
# E1. Multiple Companies at Same Address
# ─────────────────────────────────────────────

async def check_address_company_density(address: str, postcode: str = "") -> dict:
    """
    Check how many companies are registered at the same address.
    200+ companies at one address = formation agent / virtual office.
    Uses Companies House search by registered office.
    """
    import os
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "source": "address_company_density",
        "address": address,
        "estimated_companies": 0,
        "signal": "unknown",
        "details": {}
    }

    search_term = postcode or address
    if not search_term:
        return result

    # Web search approach (works without API key)
    try:
        results = await _search_ddg(
            f'"{search_term}" site:find-and-update.company-information.service.gov.uk',
            max_results=5
        )
        company_count = 0
        for r in results:
            if isinstance(r, dict) and "body" in r:
                # Look for "X companies found" pattern
                body = r.get("body", "")
                count_match = re.search(r'(\d+)\s*compan', body.lower())
                if count_match:
                    company_count = max(company_count, int(count_match.group(1)))

        result["estimated_companies"] = company_count
        if company_count > 200:
            result["signal"] = "formation_agent_address"
        elif company_count > 50:
            result["signal"] = "likely_virtual_office"
        elif company_count > 10:
            result["signal"] = "shared_office_space"
        elif company_count > 0:
            result["signal"] = "low_density"
        else:
            result["signal"] = "no_data"

    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# E3. Postcode Validation
# ─────────────────────────────────────────────

async def validate_postcode(postcode: str) -> dict:
    """
    Validate a UK postcode and get geographic details.
    Free API: https://postcodes.io/
    Returns: latitude, longitude, region, country, admin district.
    """
    result = {
        "source": "postcode_validation",
        "postcode": postcode,
        "valid": False,
        "details": {}
    }

    clean_pc = postcode.strip().upper()

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"https://api.postcodes.io/postcodes/{clean_pc}",
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 200:
                    pc_data = data.get("result", {})
                    result["valid"] = True
                    result["details"] = {
                        "postcode": pc_data.get("postcode", ""),
                        "region": pc_data.get("region", ""),
                        "country": pc_data.get("country", ""),
                        "admin_district": pc_data.get("admin_district", ""),
                        "parish": pc_data.get("parish", ""),
                        "latitude": pc_data.get("latitude"),
                        "longitude": pc_data.get("longitude"),
                        "parliamentary_constituency": pc_data.get("parliamentary_constituency", ""),
                    }
            elif resp.status_code == 404:
                result["details"]["note"] = "Postcode not found — may be invalid"
        except Exception as e:
            result["error"] = str(e)

    return result
